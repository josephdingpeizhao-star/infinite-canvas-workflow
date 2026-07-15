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
