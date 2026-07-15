from __future__ import annotations

import hashlib
import json
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
    artifact_file_under_root,
    build_final_prompt_batch_prompt,
    load_skill_runtime_package,
    load_typed_artifact,
    parse_user_confirmed_requirements,
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
    ) -> str:
        return build_final_prompt_batch_prompt(
            mode=mode,
            product_id="p1",
            repository_root=ROOT,
            identity={"artifact_type": "product_identity_archive"},
            style_master={"artifact_type": "style_master"},
            angle_inventory=angle_inventory or final_prompt_angle_inventory(),
            variable_config=variable_config or final_prompt_variable_config(mode, enabled_ids),
            requirements=parse_user_confirmed_requirements({"notes": NOTES}),
        )

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
