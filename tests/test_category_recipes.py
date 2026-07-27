from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import (  # noqa: E402
    CategoryRecipeError,
    installed_category_metadata,
    load_category_recipe,
    load_manifest_category,
)
from codex_dev_downstream import (  # noqa: E402
    _reject_unsupported_claims,
    build_variable_config_prompt,
    parse_user_confirmed_requirements,
)
from executor_contract import ExecutorExecutionError  # noqa: E402


PLATE_FACTS = {
    "product_type": "盘子",
    "length_cm": 30,
    "width_cm": 28,
    "height_cm": 4,
    "handheld_main": 0,
    "handheld_detail": 8,
    "allow_clear_water": False,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}


class CategoryRecipeTest(unittest.TestCase):
    def copy_recipes(self, root: Path) -> None:
        shutil.copytree(ROOT / "categories", root / "categories")

    def test_installed_metadata_is_recipe_driven_and_does_not_expose_internal_paths(self) -> None:
        metadata = installed_category_metadata(ROOT)

        self.assertEqual(["杯类", "盘子"], [item["key"] for item in metadata])
        plate = next(item for item in metadata if item["key"] == "盘子")
        self.assertEqual("盘子", plate["product_noun"])
        self.assertEqual(
            ["length_cm", "width_cm", "height_cm"],
            plate["form"]["dimensions"]["required"],
        )
        serialized = json.dumps(metadata, ensure_ascii=False)
        self.assertNotIn("content_sha256", serialized)
        self.assertNotIn("business_review_status", serialized)
        self.assertNotIn(str(ROOT), serialized)

    def test_plate_recipe_has_explicit_business_review_gate_and_full_lexicons(self) -> None:
        recipe = load_category_recipe(ROOT, "盘子")

        self.assertEqual("pending_business_review", recipe.business_review_status)
        self.assertIn("盘身", recipe.lexicons["protected_structure_terms"])
        self.assertIn("盘面", recipe.lexicons["product_material_context_markers"])
        self.assertIn("碟身", recipe.lexicons["product_material_context_markers"])
        self.assertIn("托盘", recipe.lexicons["ambiguous_product_or_prop_terms"])
        self.assertIn("plate-qc-identity", json.dumps(recipe.runtime_packages, ensure_ascii=False))

    def test_old_manifest_without_category_uses_cup_recipe_without_rewrite(self) -> None:
        manifest = {
            "user_confirmed_facts": {
                "product_type": "水壶",
                "height_cm": 25,
                "handheld_main": 2,
                "handheld_detail": 1,
                "allow_clear_water": True,
                "forbid_pouring_and_heating": True,
                "missing_d_no_retake": True,
            }
        }

        requirements = parse_user_confirmed_requirements(manifest, ROOT)

        self.assertEqual("杯类", requirements.category)
        self.assertEqual("杯类", load_manifest_category(ROOT, manifest).key)
        self.assertNotIn("category", manifest)

    def test_unknown_missing_and_malformed_recipe_fail_closed(self) -> None:
        with self.assertRaises(CategoryRecipeError):
            load_category_recipe(ROOT, "不存在")

        for mutation in ("missing", "malformed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_recipes(root)
                target = root / "categories" / "盘子" / "lexicons.json"
                if mutation == "missing":
                    target.unlink()
                else:
                    target.write_text("{", encoding="utf-8")
                with self.assertRaises(CategoryRecipeError):
                    load_category_recipe(root, "盘子")

    def test_recipe_cache_refreshes_after_a_valid_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.copy_recipes(root)
            before = load_category_recipe(root, "盘子")
            form_path = root / "categories" / "盘子" / "form.json"
            form = json.loads(form_path.read_text(encoding="utf-8"))
            form["dimensions"]["fields"][0]["label"] = "产品长"
            form_path.write_text(
                json.dumps(form, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            after = load_category_recipe(root, "盘子")

            self.assertNotEqual(before.content_sha256, after.content_sha256)
            self.assertEqual("产品长", after.form["dimensions"]["fields"][0]["label"])

    def test_dimension_requirements_and_handheld_boundaries_come_from_recipe(self) -> None:
        for main, detail in ((0, 0), (6, 8)):
            with self.subTest(main=main, detail=detail):
                facts = {**PLATE_FACTS, "handheld_main": main, "handheld_detail": detail}
                parsed = parse_user_confirmed_requirements(
                    {"category": "盘子", "user_confirmed_facts": facts},
                    ROOT,
                )
                self.assertEqual((main, detail), (parsed.handheld_main, parsed.handheld_detail))

        invalid_values = (
            ("length_cm", None),
            ("width_cm", None),
            ("height_cm", None),
            ("length_cm", 0),
            ("width_cm", -1),
            ("height_cm", True),
            ("handheld_main", -1),
            ("handheld_main", 7),
            ("handheld_detail", 9),
            ("handheld_detail", 1.5),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                facts = {**PLATE_FACTS, field: value}
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(
                        {"category": "盘子", "user_confirmed_facts": facts},
                        ROOT,
                    )

    def test_explicit_category_requires_exact_nine_fields_and_recipe_product_noun(self) -> None:
        invalid = (
            {key: value for key, value in PLATE_FACTS.items() if key != "width_cm"},
            {**PLATE_FACTS, "unexpected": True},
        )
        for facts in invalid:
            with self.subTest(facts=facts):
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(
                        {"category": "盘子", "user_confirmed_facts": facts},
                        ROOT,
                    )

    def test_plate_prompt_and_material_gate_use_plate_recipe(self) -> None:
        requirements = parse_user_confirmed_requirements(
            {"category": "盘子", "user_confirmed_facts": PLATE_FACTS},
            ROOT,
        )
        prompt = build_variable_config_prompt(
            mode="main",
            product_id="plate_fixture",
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
            requirements=requirements,
        )

        self.assertIn("长约 30 厘米、宽约 28 厘米、高约 4 厘米", prompt)
        self.assertIn("盘面主图案、盘心或盘沿轮廓", prompt)
        self.assertIn("plate-main-identity-and-angle", prompt)
        self.assertIn("恰好 0 项启用手持", prompt)
        with self.assertRaisesRegex(ExecutorExecutionError, "未确认商品事实"):
            _reject_unsupported_claims(
                {"configs": [{"per_image_overrides": {"展示重点": "盘面为陶瓷"}}]},
                requirements.height_cm,
                "盘子主图变量配置",
                product_type=requirements.product_type,
                lexicons=requirements.recipe.lexicons,
                confirmed_dimensions={
                    "length_cm": 30,
                    "width_cm": 28,
                    "height_cm": 4,
                },
            )

        safe_prop = copy.deepcopy(
            {"configs": [{"per_image_overrides": {"道具生成": "后景玻璃花瓶"}}]}
        )
        _reject_unsupported_claims(
            safe_prop,
            requirements.height_cm,
            "盘子主图变量配置",
            product_type=requirements.product_type,
            lexicons=requirements.recipe.lexicons,
            confirmed_dimensions={
                "length_cm": 30,
                "width_cm": 28,
                "height_cm": 4,
            },
        )

    def test_cup_optional_length_and_width_add_recipe_text_without_changing_height_only(self) -> None:
        facts = {
            "product_type": "杯子",
            "length_cm": 12,
            "width_cm": 10,
            "height_cm": 25,
            "handheld_main": 2,
            "handheld_detail": 1,
            "allow_clear_water": True,
            "forbid_pouring_and_heating": True,
            "missing_d_no_retake": True,
        }
        requirements = parse_user_confirmed_requirements(
            {"category": "杯类", "user_confirmed_facts": facts},
            ROOT,
        )
        prompt = build_variable_config_prompt(
            mode="main",
            product_id="cup_optional_dimensions",
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
            requirements=requirements,
        )

        self.assertIn("长约 12 厘米、宽约 10 厘米", prompt)
        self.assertIn("不得改写已确认高度", prompt)

    def test_plate_cli_requires_three_dimensions_and_keeps_dry_run_side_effect_free(self) -> None:
        product_id = "__cat01_plate_cli_dry_run__"
        command = [
            sys.executable,
            str(ROOT / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            product_id,
            "--category",
            "盘子",
            "--product-type",
            "盘子",
            "--length-cm",
            "30",
            "--width-cm",
            "28",
            "--height-cm",
            "4",
            "--handheld-main",
            "6",
            "--handheld-detail",
            "8",
            "--allow-clear-water",
            "false",
            "--forbid-pouring-and-heating",
            "true",
            "--missing-d-no-retake",
            "true",
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
        manifest = json.loads(completed.stdout)["manifest_data"]
        self.assertEqual("盘子", manifest["category"])
        self.assertEqual(
            PLATE_FACTS
            | {
                "main_image_count": 6,
                "detail_image_count": 8,
                "handheld_main": 6,
            },
            manifest["user_confirmed_facts"],
        )
        self.assertFalse((ROOT / "manifests" / f"{product_id}.batch_manifest.json").exists())

        missing_width = [item for item in command if item not in {"--width-cm", "28"}]
        rejected = subprocess.run(
            missing_width,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertNotEqual(0, rejected.returncode)

    def test_runtime_code_reads_only_generic_skill_files_from_agents(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                BRIDGE / "codex_dev_executor.py",
                BRIDGE / "codex_dev_downstream.py",
                BRIDGE / "codex_dev_qc.py",
            )
        )

        self.assertNotIn('".agents" / "skills" / skill_name / "references"', sources)
        self.assertNotIn('skill_root / "references"', sources)
        self.assertIn('"SKILL.md"', sources)


if __name__ == "__main__":
    unittest.main()
