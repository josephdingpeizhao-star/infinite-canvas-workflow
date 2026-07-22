from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_downstream import (  # noqa: E402
    _reject_scene_policy_violations,
    _reject_unsupported_claims,
    _validate_bound_angle,
    artifact_file_under_root,
    build_final_prompt_batch_prompt,
    build_variable_config_prompt,
    load_skill_runtime_package,
    load_typed_artifact,
    parse_final_prompt_batch_response,
    parse_user_confirmed_requirements,
    parse_variable_config_response,
    qualified_angle_assets,
    stable_json_sha256,
    write_bundle_exclusive,
    write_json_exclusive,
)
from executor_contract import ExecutorExecutionError  # noqa: E402


NOTES = (
    "用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | "
    "主图手持数量: 2 | 详情图手持数量: 1 | "
    "允许清水场景: 是 | 禁止倾倒与加热: 是 | D槽位不补拍: 是"
)

STRUCTURED_FACTS = {
    "product_type": "家居盛水水壶",
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}

SEMANTIC_GATE_FIXTURE = ROOT / "tests" / "fixtures" / "main_vc_reply_20260721_semantic_gate.json"
SEMANTIC_GATE_20260722_FIXTURE = (
    ROOT / "tests" / "fixtures" / "main_vc_reply_20260722_semantic_gate.json"
)
STYLE_MASTER_GLASS_PROP_TEXT = (
    "后景可有柔和虚化的玻璃器皿、植物、玻璃花器和玻璃花瓶。"
)
ANGLE_SLOT_LITERAL_CONTRACT = (
    "每项“绑定角度槽位”字段必须同时写出唯一合格源图编号，并原样包含“X 槽位”或“槽位 X”字样；"
    "X 必须是该源图实际对应的 A/B/C 槽位。"
)


def semantic_gate_requirements():
    return parse_user_confirmed_requirements(
        {
            "user_confirmed_facts": {
                "product_type": "杯子",
                "height_cm": 8,
                "handheld_main": 2,
                "handheld_detail": 1,
                "allow_clear_water": True,
                "forbid_pouring_and_heating": True,
                "missing_d_no_retake": True,
            }
        }
    )


def semantic_gate_angle_inventory() -> dict[str, object]:
    return {
        "angle_slots": [
            {
                "source_asset_id": "img_002",
                "angle_slot": "A",
                "admission_result": "合格，可进入对应槽位",
            },
            {
                "source_asset_id": "img_001",
                "angle_slot": "B",
                "admission_result": "合格，可进入对应槽位",
            },
        ],
        "missing_angle_slots": ["D"],
    }


def semantic_gate_20260722_angle_inventory() -> dict[str, object]:
    inventory = semantic_gate_angle_inventory()
    inventory["angle_slots"][0]["angle_slot"] = "C"
    return inventory


def semantic_gate_response() -> dict[str, object]:
    return json.loads(SEMANTIC_GATE_FIXTURE.read_text(encoding="utf-8"))


def semantic_gate_response_with_literal_slots() -> dict[str, object]:
    response = copy.deepcopy(semantic_gate_response())
    for index, config in enumerate(response["configs"]):
        binding = config["per_image_overrides"]["绑定角度槽位"]
        slot = binding[0]
        replacement = f"{slot} 槽位" if index % 2 == 0 else f"槽位 {slot}"
        config["per_image_overrides"]["绑定角度槽位"] = replacement + binding[1:]
    return response


def semantic_gate_20260722_response() -> dict[str, object]:
    text = SEMANTIC_GATE_20260722_FIXTURE.read_text(encoding="utf-8")
    return json.loads(text[text.index("{") :])


def replace_corresponding_product_with_product_id(
    response: dict[str, object],
    product_id: str = "杯子_20260722",
) -> dict[str, object]:
    updated = copy.deepcopy(response)
    for config in updated["configs"]:
        overrides = config["per_image_overrides"]
        overrides["辅助参考图调用"] = overrides["辅助参考图调用"].replace(
            "对应产品：竖条纹陶瓷马克杯",
            f"对应产品：{product_id}",
        )
    return updated


def write_style_master_fixture(
    root: Path,
    *,
    product_id: str,
    text: str = STYLE_MASTER_GLASS_PROP_TEXT,
) -> Path:
    path = root / "style_master.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "style_master",
                "product_id": product_id,
                "style_master": {"prop_rules": text},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


FINAL_PROMPT_BINDINGS = {
    "main": (
        ("img_002", "A"),
        ("img_006", "B"),
        ("img_007", "C"),
        ("img_003", "A"),
        ("img_006", "B"),
        ("img_004", "A"),
    ),
    "detail": (
        ("img_002", "A"),
        ("img_006", "B"),
        ("img_007", "C"),
        ("img_003", "A"),
        ("img_004", "A"),
        ("img_001", "A"),
        ("img_006", "B"),
        ("img_002", "A"),
    ),
}


def final_prompt_angle_inventory() -> dict[str, object]:
    records = [
        {
            "source_asset_id": asset_id,
            "angle_slot": slot,
            "admission_result": "合格，可进入对应槽位",
        }
        for asset_id, slot in (
            ("img_001", "A"),
            ("img_002", "A"),
            ("img_003", "A"),
            ("img_004", "A"),
            ("img_006", "B"),
            ("img_007", "C"),
        )
    ]
    records.extend(
        {
            "source_asset_id": asset_id,
            "angle_slot": "不适合归入现有槽位",
            "admission_result": "不适合入库，需重拍",
        }
        for asset_id in ("img_005", "img_008", "img_009", "img_010", "img_011", "img_012")
    )
    return {"angle_slots": records, "missing_angle_slots": ["D"]}


def final_prompt_variable_config(mode: str, enabled_ids: set[str]) -> dict[str, object]:
    common = {
        "产品类型": "家居盛水水壶",
        "已确认高度": "约 25 厘米",
        "动作边界": "允许清水静置；禁止倾倒、加热、沸腾或热水动作",
    }
    configs: list[dict[str, object]] = []
    for index, (asset_id, slot) in enumerate(FINAL_PROMPT_BINDINGS[mode], start=1):
        config_id = f"{mode}_{index:02d}"
        overrides = {
            "绑定角度槽位": f"{slot} 槽位，绑定源图 {asset_id}；本张仅调用这一张白底图。",
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：静态握持"
                if config_id in enabled_ids
                else "本张图不启用手持场景"
            ),
        }
        resolved = dict(common)
        resolved.update(overrides)
        configs.append(
            {
                "config_id": config_id,
                "output_type": mode,
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "正式变量配置测试夹具",
            }
        )
    return {
        "product_id": "p1",
        "artifact_type": f"{mode}_variable_config",
        "config_count": len(configs),
        "upstream_artifacts": {},
        "common_constraints": common,
        "configs": configs,
        "notes": "正式变量配置测试夹具",
    }


class CodexDevDownstreamTest(unittest.TestCase):
    def make_manifest(self, root: Path) -> dict[str, object]:
        artifacts_root = root / "workspace" / "artifacts"
        return {
            "product_id": "p1",
            "notes": NOTES,
            "workspace": {"artifacts_root": str(artifacts_root)},
            "artifacts": {
                "main_variable_configs": [str(artifacts_root / "variable_configs")],
                "product_identity_archive": str(artifacts_root / "identity"),
            },
        }

    def build_final_prompt(
        self,
        mode: str,
        enabled_ids: set[str],
        *,
        angle_inventory: dict[str, object] | None = None,
        variable_config: dict[str, object] | None = None,
        requirements_manifest: dict[str, object] | None = None,
    ) -> str:
        return build_final_prompt_batch_prompt(
            mode=mode,
            product_id="p1",
            repository_root=ROOT,
            identity={"artifact_type": "product_identity_archive"},
            style_master={"artifact_type": "style_master"},
            angle_inventory=angle_inventory or final_prompt_angle_inventory(),
            variable_config=variable_config or final_prompt_variable_config(mode, enabled_ids),
            requirements=parse_user_confirmed_requirements(
                requirements_manifest or {"notes": NOTES}
            ),
        )

    def test_real_main_vc_fixture_accepts_non_product_material_props(self) -> None:
        response = semantic_gate_response()

        _reject_unsupported_claims(response, 8, "主图变量配置")

    def test_20260722_fixture_allows_non_product_materials_without_phrase_allowlist(self) -> None:
        response = replace_corresponding_product_with_product_id(
            semantic_gate_20260722_response()
        )

        _reject_unsupported_claims(
            response,
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_20260722_fixture_still_rejects_only_six_ceramic_product_mentions(self) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            _reject_unsupported_claims(
                semantic_gate_20260722_response(),
                8,
                "主图变量配置",
                product_type="杯子",
                style_master_text=STYLE_MASTER_GLASS_PROP_TEXT,
            )

        detail = str(caught.exception)
        self.assertIn("未确认商品事实（6 处：", detail)
        self.assertIn("configs/0/per_image_overrides/辅助参考图调用", detail)
        self.assertNotIn("道具生成", detail)
        self.assertNotIn("背景层次配置", detail)

    def test_20260722_fixture_with_product_id_passes_main_full_validation(self) -> None:
        response = replace_corresponding_product_with_product_id(
            semantic_gate_20260722_response()
        )
        with tempfile.TemporaryDirectory() as tmp:
            style_path = write_style_master_fixture(
                Path(tmp),
                product_id="杯子_20260722",
            )

            artifact = parse_variable_config_response(
                json.dumps(response, ensure_ascii=False),
                mode="main",
                product_id="杯子_20260722",
                requirements=semantic_gate_requirements(),
                angle_inventory=semantic_gate_20260722_angle_inventory(),
                upstream_paths={
                    "product_identity_archive": Path("identity.json"),
                    "style_master": style_path,
                    "angle_inventory": Path("angles.json"),
                },
            )

        self.assertEqual("main_variable_config", artifact["artifact_type"])
        self.assertEqual(6, artifact["config_count"])

    def test_non_product_glass_vessel_defaults_to_allow(self) -> None:
        _reject_unsupported_claims(
            {"per_image_overrides": {"道具生成": "后景玻璃器皿虚化"}},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_bare_non_product_material_word_defaults_to_allow(self) -> None:
        _reject_unsupported_claims(
            {"per_image_overrides": {"道具生成": "后景使用玻璃"}},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_absent_non_product_material_phrase_defaults_to_allow(self) -> None:
        _reject_unsupported_claims(
            {"per_image_overrides": {"道具生成": "后景使用玻璃雕塑"}},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_product_directed_material_contract_rejects_tight_coupling(self) -> None:
        for claim in ("杯身为玻璃", "玻璃质感壶身"):
            with self.subTest(claim=claim):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    _reject_unsupported_claims(
                        {"per_image_overrides": {"道具生成": claim}},
                        8,
                        "主图变量配置",
                        product_type="杯子",
                    )
                self.assertIn("未确认商品事实", str(caught.exception))

    def test_non_product_material_in_positive_field_defaults_to_allow(self) -> None:
        _reject_unsupported_claims(
            {"per_image_overrides": {"展示重点": "后景玻璃器皿虚化"}},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_real_main_vc_fixture_accepts_unarranged_prohibited_actions(self) -> None:
        requirements = semantic_gate_requirements()

        _reject_scene_policy_violations(
            semantic_gate_response(),
            requirements,
            "主图变量配置",
        )
        _reject_scene_policy_violations(
            {"notes": "禁止在保持原角度和自然静态握持的长句说明中安排倾倒、沸腾或炉灶加热"},
            requirements,
            "主图变量配置",
        )

    def test_prop_material_context_does_not_hide_product_material_claims(self) -> None:
        for claim in (
            "杯身为玻璃材质",
            "玻璃质感壶身",
            "产品采用陶瓷",
            "产品采用玻璃花瓶造型",
        ):
            with self.subTest(claim=claim):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    _reject_unsupported_claims(
                        {"per_image_overrides": {"展示重点": claim}},
                        8,
                        "主图变量配置",
                    )
                self.assertIn("未确认商品事实", str(caught.exception))

    def test_scene_negation_predicates_do_not_hide_positive_actions(self) -> None:
        requirements = semantic_gate_requirements()
        for claim in (
            "本张安排倾倒展示",
            "安排自然握持，随后执行倾倒",
            "未确认背景节奏，本张安排炉灶加热",
        ):
            with self.subTest(claim=claim):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    _reject_scene_policy_violations(
                        {"notes": claim},
                        requirements,
                        "主图变量配置",
                    )
                self.assertIn("违反用户确认场景边界", str(caught.exception))

    def test_main_variable_prompt_requires_literal_angle_slot_contract(self) -> None:
        prompt = build_variable_config_prompt(
            mode="main",
            product_id="杯子_20260719",
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=semantic_gate_angle_inventory(),
            requirements=semantic_gate_requirements(),
        )

        self.assertIn(ANGLE_SLOT_LITERAL_CONTRACT, prompt)

    def test_main_variable_prompt_requires_product_id_only_corresponding_product_contract(self) -> None:
        prompt = build_variable_config_prompt(
            mode="main",
            product_id="杯子_20260722",
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=semantic_gate_angle_inventory(),
            requirements=semantic_gate_requirements(),
        )

        self.assertIn(
            "主图每项“辅助参考图调用”中的“对应产品”必须只原样填写本批 "
            "product_id：杯子_20260722；不得填写产品外观、材质、品类昵称或其他描述性名称。",
            prompt,
        )

    def test_detail_variable_prompt_requires_literal_angle_slot_contract(self) -> None:
        prompt = build_variable_config_prompt(
            mode="detail",
            product_id="杯子_20260719",
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=semantic_gate_angle_inventory(),
            requirements=semantic_gate_requirements(),
            main_variable_config=semantic_gate_response_with_literal_slots(),
        )

        self.assertIn(ANGLE_SLOT_LITERAL_CONTRACT, prompt)

    def test_detail_variable_prompt_requires_product_id_only_corresponding_product_contract(self) -> None:
        prompt = build_variable_config_prompt(
            mode="detail",
            product_id="杯子_20260722",
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=semantic_gate_angle_inventory(),
            requirements=semantic_gate_requirements(),
            main_variable_config=semantic_gate_response_with_literal_slots(),
        )

        self.assertIn(
            "详情图每项“辅助参考图调用”中的“对应产品”必须只原样填写本批 "
            "product_id：杯子_20260722；不得填写产品外观、材质、品类昵称或其他描述性名称。",
            prompt,
        )

    def test_real_main_fixture_with_literal_slots_passes_full_validation(self) -> None:
        style_root = tempfile.TemporaryDirectory()
        self.addCleanup(style_root.cleanup)
        style_path = write_style_master_fixture(
            Path(style_root.name),
            product_id="杯子_20260719",
        )
        artifact = parse_variable_config_response(
            json.dumps(semantic_gate_response_with_literal_slots(), ensure_ascii=False),
            mode="main",
            product_id="杯子_20260719",
            requirements=semantic_gate_requirements(),
            angle_inventory=semantic_gate_angle_inventory(),
            upstream_paths={
                "product_identity_archive": Path("identity.json"),
                "style_master": style_path,
                "angle_inventory": Path("angles.json"),
            },
        )

        self.assertEqual("main_variable_config", artifact["artifact_type"])
        self.assertEqual(6, artifact["config_count"])

    def test_variable_config_fails_closed_when_style_master_is_unreadable(self) -> None:
        response = replace_corresponding_product_with_product_id(
            semantic_gate_20260722_response()
        )

        with self.assertRaises(ExecutorExecutionError) as caught:
            parse_variable_config_response(
                json.dumps(response, ensure_ascii=False),
                mode="main",
                product_id="杯子_20260722",
                requirements=semantic_gate_requirements(),
                angle_inventory=semantic_gate_angle_inventory(),
                upstream_paths={
                    "product_identity_archive": Path("identity.json"),
                    "style_master": Path("missing-style-master.json"),
                    "angle_inventory": Path("angles.json"),
                },
            )

        self.assertEqual(
            "codex-dev 无法读取有效的正式风格母版",
            str(caught.exception),
        )

    def test_bound_angle_still_rejects_missing_literal_and_d_slot(self) -> None:
        qualified = qualified_angle_assets(semantic_gate_angle_inventory())
        cases = (
            ("A / img_002，正面微俯视", "角度绑定异常"),
            ("img_002；A 槽位；D 槽位", "使用了缺失的 D 槽位"),
        )
        for binding, expected in cases:
            with self.subTest(binding=binding):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    _validate_bound_angle(binding, qualified, "主图变量配置")
                self.assertIn(expected, str(caught.exception))

    def test_final_prompt_builder_lists_main_handheld_literal_contract_per_config(self) -> None:
        prompt = self.build_final_prompt("main", {"main_02", "main_05"})

        for config_id in ("main_02", "main_05"):
            self.assertIn(
                f"- {config_id}：final_prompt 正文必须原样出现完整肯定短语“启用手持场景”，"
                "且该正文不得出现完整否定短语“本张图不启用手持场景”。",
                prompt,
            )
        for config_id in ("main_01", "main_03", "main_04", "main_06"):
            self.assertIn(
                f"- {config_id}：final_prompt 正文必须原样出现完整否定短语"
                "“本张图不启用手持场景”。",
                prompt,
            )

    def test_final_prompt_builder_lists_detail_handheld_literal_contract_per_config(self) -> None:
        prompt = self.build_final_prompt("detail", {"detail_02"})

        self.assertIn(
            "- detail_02：final_prompt 正文必须原样出现完整肯定短语“启用手持场景”，"
            "且该正文不得出现完整否定短语“本张图不启用手持场景”。",
            prompt,
        )
        for index in (1, 3, 4, 5, 6, 7, 8):
            self.assertIn(
                f"- detail_{index:02d}：final_prompt 正文必须原样出现完整否定短语"
                "“本张图不启用手持场景”。",
                prompt,
            )

    def test_final_prompt_builder_lists_bound_asset_and_slot_contract_per_config(self) -> None:
        enabled = {"main": {"main_02", "main_05"}, "detail": {"detail_02"}}
        for mode in ("main", "detail"):
            prompt = self.build_final_prompt(mode, enabled[mode])
            for index, (asset_id, slot) in enumerate(FINAL_PROMPT_BINDINGS[mode], start=1):
                with self.subTest(mode=mode, index=index):
                    self.assertIn(
                        f"- {mode}_{index:02d}：final_prompt 正文必须原样出现源图编号“{asset_id}”，"
                        f"并且必须原样出现“{slot} 槽位”或“槽位 {slot}”中的至少一种。",
                        prompt,
                    )

    def test_final_prompt_builder_derives_handheld_contract_from_variable_config(self) -> None:
        prompt = self.build_final_prompt("main", {"main_01", "main_03"})

        for config_id in ("main_01", "main_03"):
            self.assertIn(
                f"- {config_id}：final_prompt 正文必须原样出现完整肯定短语“启用手持场景”，"
                "且该正文不得出现完整否定短语“本张图不启用手持场景”。",
                prompt,
            )
        for config_id in ("main_02", "main_05"):
            self.assertIn(
                f"- {config_id}：final_prompt 正文必须原样出现完整否定短语"
                "“本张图不启用手持场景”。",
                prompt,
            )
            self.assertNotIn(
                f"- {config_id}：final_prompt 正文必须原样出现完整肯定短语“启用手持场景”",
                prompt,
            )

    def test_final_prompt_builder_preserves_existing_batch_requirements(self) -> None:
        for mode, enabled_ids, ratio, handheld_count in (
            ("main", {"main_02", "main_05"}, "1:1", 2),
            ("detail", {"detail_02"}, "3:4", 1),
        ):
            with self.subTest(mode=mode):
                prompt = self.build_final_prompt(mode, enabled_ids)
                self.assertIn(f"画布比例 {ratio}", prompt)
                self.assertIn("产品高度约 25 厘米", prompt)
                self.assertIn(f"恰好 {handheld_count} 份保持启用手持", prompt)
                self.assertIn("禁止 D、被拒绝源图、倾倒、加热", prompt)
                self.assertIn("必须且只返回这些配置", prompt)
                self.assertIn("只返回一个 JSON 对象", prompt)

    def test_final_prompt_builder_applies_scene_switches_without_action_authorization(self) -> None:
        no_clear_or_action_ban = self.build_final_prompt(
            "main",
            {"main_02", "main_05"},
            requirements_manifest={
                "user_confirmed_facts": {
                    **STRUCTURED_FACTS,
                    "allow_clear_water": False,
                    "forbid_pouring_and_heating": False,
                }
            },
        )
        clear_allowed_without_action_ban = self.build_final_prompt(
            "main",
            {"main_02", "main_05"},
            requirements_manifest={
                "user_confirmed_facts": {
                    **STRUCTURED_FACTS,
                    "forbid_pouring_and_heating": False,
                }
            },
        )

        self.assertIn("禁止 D、被拒绝源图、清水场景，以及容量", no_clear_or_action_ban)
        self.assertIn("禁止 D、被拒绝源图，以及容量", clear_allowed_without_action_ban)
        for prompt in (no_clear_or_action_ban, clear_allowed_without_action_ban):
            self.assertNotIn("禁止 D、被拒绝源图、倾倒、加热", prompt)

    def test_final_prompt_builder_disambiguates_negative_handheld_substring(self) -> None:
        prompt = self.build_final_prompt("main", {"main_02", "main_05"})

        self.assertIn(
            "注意：“本张图不启用手持场景”包含“启用手持场景”作为子串。"
            "对启用手持的配置，完整否定短语一旦出现即为不合格，"
            "不能用其中的肯定子串充当肯定要求。",
            prompt,
        )

    def test_final_prompt_builder_rejects_binding_with_no_qualified_asset(self) -> None:
        variable_config = final_prompt_variable_config("main", {"main_02", "main_05"})
        first = variable_config["configs"][0]
        first["per_image_overrides"]["绑定角度槽位"] = (
            "PRIVATE_BINDING；A 槽位，绑定源图 img_999"
        )
        resolved = dict(variable_config["common_constraints"])
        resolved.update(first["per_image_overrides"])
        first["resolved_variable_config_sha256"] = stable_json_sha256(resolved)

        with self.assertRaises(ExecutorExecutionError) as caught:
            self.build_final_prompt(
                "main",
                {"main_02", "main_05"},
                variable_config=variable_config,
            )

        self.assertEqual("codex-dev 收到的最终提示词编译角度绑定异常", str(caught.exception))
        self.assertNotIn("PRIVATE_BINDING", str(caught.exception))

    def test_final_prompt_builder_rejects_binding_with_multiple_qualified_assets(self) -> None:
        variable_config = final_prompt_variable_config("main", {"main_02", "main_05"})
        first = variable_config["configs"][0]
        first["per_image_overrides"]["绑定角度槽位"] = (
            "PRIVATE_BINDING；A 槽位，绑定源图 img_001 和 img_002"
        )
        resolved = dict(variable_config["common_constraints"])
        resolved.update(first["per_image_overrides"])
        first["resolved_variable_config_sha256"] = stable_json_sha256(resolved)

        with self.assertRaises(ExecutorExecutionError) as caught:
            self.build_final_prompt(
                "main",
                {"main_02", "main_05"},
                variable_config=variable_config,
            )

        self.assertEqual("codex-dev 收到的最终提示词编译角度绑定异常", str(caught.exception))
        self.assertNotIn("PRIVATE_BINDING", str(caught.exception))

    def test_user_requirements_are_parsed_from_manifest_notes(self) -> None:
        requirements = parse_user_confirmed_requirements({"notes": NOTES})

        self.assertEqual("水壶", requirements.product_type)
        self.assertEqual(25, requirements.height_cm)
        self.assertEqual(2, requirements.handheld_main)
        self.assertEqual(1, requirements.handheld_detail)
        self.assertTrue(requirements.allow_clear_water)
        self.assertTrue(requirements.forbid_pouring_and_heating)
        self.assertTrue(requirements.missing_d_no_retake)

    def test_invalid_user_requirements_are_rejected_without_echoing_notes(self) -> None:
        secret_notes = "用户确认产品类型: 水壶 | secret-token-123"

        with self.assertRaises(ExecutorExecutionError) as caught:
            parse_user_confirmed_requirements({"notes": secret_notes})

        self.assertEqual("codex-dev 缺少有效的用户确认商品信息", str(caught.exception))
        self.assertNotIn("secret-token-123", str(caught.exception))

    def test_structured_user_requirements_take_precedence_over_notes(self) -> None:
        manifest = {
            "notes": NOTES,
            "user_confirmed_facts": {
                **STRUCTURED_FACTS,
                "product_type": "玻璃收纳罐",
                "height_cm": 31,
                "allow_clear_water": False,
            },
        }

        requirements = parse_user_confirmed_requirements(manifest)

        self.assertEqual("玻璃收纳罐", requirements.product_type)
        self.assertEqual(31, requirements.height_cm)
        self.assertFalse(requirements.allow_clear_water)

    def test_structured_user_requirements_require_exact_keys_and_types(self) -> None:
        invalid_facts = (
            {key: value for key, value in STRUCTURED_FACTS.items() if key != "height_cm"},
            {**STRUCTURED_FACTS, "unexpected": "private-value"},
            {**STRUCTURED_FACTS, "product_type": "   "},
            {**STRUCTURED_FACTS, "height_cm": True},
            {**STRUCTURED_FACTS, "handheld_main": 3},
            {**STRUCTURED_FACTS, "handheld_detail": 2},
            {**STRUCTURED_FACTS, "allow_clear_water": 1},
            {**STRUCTURED_FACTS, "forbid_pouring_and_heating": "是"},
            {**STRUCTURED_FACTS, "missing_d_no_retake": None},
        )
        for facts in invalid_facts:
            with self.subTest(facts=facts):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    parse_user_confirmed_requirements(
                        {"notes": NOTES, "user_confirmed_facts": facts}
                    )
                self.assertEqual(
                    "codex-dev 缺少有效的用户确认商品信息",
                    str(caught.exception),
                )
                self.assertNotIn("private-value", str(caught.exception))

    def test_legacy_notes_accept_any_nonempty_product_type_and_exact_booleans(self) -> None:
        requirements = parse_user_confirmed_requirements(
            {
                "notes": (
                    "用户确认产品类型: 玻璃收纳罐 | 用户确认高度厘米: 31 | "
                    "主图手持数量: 2 | 详情图手持数量: 1 | "
                    "允许清水场景: 否 | 禁止倾倒与加热: 否 | D槽位不补拍: 否"
                )
            }
        )

        self.assertEqual("玻璃收纳罐", requirements.product_type)
        self.assertEqual(31, requirements.height_cm)
        self.assertFalse(requirements.allow_clear_water)
        self.assertFalse(requirements.forbid_pouring_and_heating)
        self.assertFalse(requirements.missing_d_no_retake)

        with self.assertRaises(ExecutorExecutionError):
            parse_user_confirmed_requirements(
                {"notes": NOTES.replace("允许清水场景: 是", "允许清水场景: 已确认")}
            )

    def test_structured_and_legacy_water_facts_compile_identical_prompt_bytes(self) -> None:
        legacy = self.build_final_prompt("main", {"main_02", "main_05"})
        structured = self.build_final_prompt(
            "main",
            {"main_02", "main_05"},
            requirements_manifest={
                "user_confirmed_facts": {**STRUCTURED_FACTS, "product_type": "水壶"}
            },
        )

        self.assertEqual(legacy.encode("utf-8"), structured.encode("utf-8"))

    def test_manifest_template_is_a_non_executable_confirmation_skeleton(self) -> None:
        template = json.loads(
            (ROOT / "manifests" / "batch_manifest.template.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            {
                "product_type": "",
                "height_cm": None,
                "handheld_main": None,
                "handheld_detail": None,
                "allow_clear_water": None,
                "forbid_pouring_and_heating": None,
                "missing_d_no_retake": None,
            },
            template["user_confirmed_facts"],
        )
        with self.assertRaises(ExecutorExecutionError):
            parse_user_confirmed_requirements(template)

    def test_manifest_builder_dry_run_emits_structured_facts_without_writing(self) -> None:
        product_id = "__structured_dry_run_test__"
        manifest_path = ROOT / "manifests" / f"{product_id}.batch_manifest.json"
        asset_manifest_path = ROOT / "manifests" / f"{product_id}.asset_manifest.json"
        input_path = ROOT / "inputs" / "products" / product_id
        artifact_path = ROOT / "artifacts" / product_id
        self.assertFalse(manifest_path.exists())
        command = [
            sys.executable,
            str(ROOT / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            product_id,
            "--product-type",
            "玻璃收纳罐",
            "--height-cm",
            "31",
            "--handheld-main",
            "2",
            "--handheld-detail",
            "1",
            "--allow-clear-water",
            "false",
            "--forbid-pouring-and-heating",
            "true",
            "--missing-d-no-retake",
            "false",
            "--dry-run",
        ]

        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            {
                **STRUCTURED_FACTS,
                "product_type": "玻璃收纳罐",
                "height_cm": 31,
                "allow_clear_water": False,
                "missing_d_no_retake": False,
            },
            result["manifest_data"]["user_confirmed_facts"],
        )
        self.assertFalse(manifest_path.exists())
        self.assertFalse(asset_manifest_path.exists())
        self.assertFalse(input_path.exists())
        self.assertFalse(artifact_path.exists())

    def test_manifest_builder_invalid_confirmation_writes_nothing(self) -> None:
        product_id = "__invalid_structured_dry_run_test__"
        manifest_path = ROOT / "manifests" / f"{product_id}.batch_manifest.json"
        asset_manifest_path = ROOT / "manifests" / f"{product_id}.asset_manifest.json"
        input_path = ROOT / "inputs" / "products" / product_id
        artifact_path = ROOT / "artifacts" / product_id
        command = [
            sys.executable,
            str(ROOT / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            product_id,
            "--product-type",
            "玻璃收纳罐",
            "--height-cm",
            "31",
            "--handheld-main",
            "3",
            "--handheld-detail",
            "1",
            "--allow-clear-water",
            "false",
            "--forbid-pouring-and-heating",
            "true",
            "--missing-d-no-retake",
            "false",
            "--dry-run",
        ]

        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(manifest_path.exists())
        self.assertFalse(asset_manifest_path.exists())
        self.assertFalse(input_path.exists())
        self.assertFalse(artifact_path.exists())

    def test_artifact_file_resolves_list_directory_under_artifacts_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.make_manifest(root)

            output = artifact_file_under_root(
                manifest,
                "main_variable_configs",
                "main_variable_configs.json",
            )

            self.assertEqual(
                root / "workspace" / "artifacts" / "variable_configs" / "main_variable_configs.json",
                output,
            )

    def test_artifact_file_rejects_path_outside_artifacts_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.make_manifest(root)
            manifest["artifacts"]["main_variable_configs"] = [str(root / "outside")]

            with self.assertRaises(ExecutorExecutionError) as caught:
                artifact_file_under_root(
                    manifest,
                    "main_variable_configs",
                    "main_variable_configs.json",
                )

            self.assertIn("artifacts_root", str(caught.exception))

    def test_typed_artifact_must_match_type_and_product(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.make_manifest(root)
            identity_dir = root / "workspace" / "artifacts" / "identity"
            identity_dir.mkdir(parents=True)
            identity_path = identity_dir / "product_identity_archive.json"
            identity_path.write_text(
                json.dumps(
                    {
                        "product_id": "p1",
                        "artifact_type": "product_identity_archive",
                        "identity": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            value, loaded_path = load_typed_artifact(
                manifest,
                "product_identity_archive",
                "product_identity_archive.json",
                "product_identity_archive",
                "产品身份档案",
            )

            self.assertEqual("p1", value["product_id"])
            self.assertEqual(identity_path, loaded_path)

            identity_path.write_text(
                json.dumps(
                    {"product_id": "other", "artifact_type": "product_identity_archive"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ExecutorExecutionError):
                load_typed_artifact(
                    manifest,
                    "product_identity_archive",
                    "product_identity_archive.json",
                    "product_identity_archive",
                    "产品身份档案",
                )

    def test_runtime_package_is_loaded_as_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = root / ".agents" / "skills" / "main-variable-config" / "references" / "runtime.json"
            package.parent.mkdir(parents=True)
            package.write_text('{"package": "main"}', encoding="utf-8")

            value = load_skill_runtime_package(
                root,
                "main-variable-config",
                "runtime.json",
                "主图变量配置",
            )

            self.assertEqual({"package": "main"}, value)

    def test_qualified_angles_exclude_rejected_assets_and_missing_d(self) -> None:
        angle_doc = {
            "angle_slots": [
                {"source_asset_id": "img_001", "angle_slot": "A", "admission_result": "合格，可进入对应槽位"},
                {"source_asset_id": "img_006", "angle_slot": "B", "admission_result": "合格，可进入对应槽位"},
                {"source_asset_id": "img_007", "angle_slot": "C", "admission_result": "基本可用，建议谨慎使用"},
                {"source_asset_id": "img_004", "angle_slot": "D", "admission_result": "合格，可进入对应槽位"},
                {"source_asset_id": "img_005", "angle_slot": "不适合归入现有槽位", "admission_result": "不适合入库，需重拍"},
            ],
            "missing_angle_slots": ["D"],
        }

        qualified = qualified_angle_assets(angle_doc)

        self.assertEqual({"img_001", "img_006", "img_007"}, set(qualified))
        self.assertNotIn("img_004", qualified)
        self.assertNotIn("img_005", qualified)
        self.assertEqual("A", qualified["img_001"]["angle_slot"])

    def test_stable_hash_uses_sorted_compact_utf8_json(self) -> None:
        value = {"b": "水壶", "a": 1}
        expected = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(expected, stable_json_sha256(value))

    def test_json_writer_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "artifact.json"
            output.write_text('{"x": 1}', encoding="utf-8")

            with self.assertRaises(ExecutorExecutionError):
                write_json_exclusive(output, {"x": 2}, "主图变量配置")

            self.assertEqual('{"x": 1}', output.read_text(encoding="utf-8"))

    def test_bundle_writer_rolls_back_only_current_call_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.json"
            second = root / "second.md"
            unrelated = root / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")

            original_link = Path.hardlink_to
            calls = 0

            def fail_second_link(path: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated link failure")
                original_link(path, target)

            with mock.patch.object(Path, "hardlink_to", fail_second_link):
                with self.assertRaises(ExecutorExecutionError):
                    write_bundle_exclusive(
                        {first: b'{"ok":true}\n', second: b"# prompt\n"},
                        "最终提示词",
                    )

            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
