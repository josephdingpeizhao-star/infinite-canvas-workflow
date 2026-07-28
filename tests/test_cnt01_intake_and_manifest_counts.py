from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import load_category_recipe  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    ExecutorExecutionError,
    parse_user_confirmed_requirements,
)
from image_count_contract import image_count_spec  # noqa: E402


OLD_CATEGORY_FACTS = {
    "product_type": "杯子",
    "length_cm": None,
    "width_cm": None,
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}
LEGACY_FACTS = {
    "product_type": "杯子",
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}


class RecipeImageCountMetadataTests(unittest.TestCase):
    def test_both_installed_categories_publish_the_approved_metadata(self) -> None:
        for category in ("杯类", "盘子", "碗"):
            with self.subTest(category=category):
                recipe = load_category_recipe(ROOT, category)
                self.assertEqual((6, 1, 30), tuple(vars(image_count_spec(recipe.form, "main")).values()))
                self.assertEqual((8, 1, 30), tuple(vars(image_count_spec(recipe.form, "detail")).values()))
                self.assertEqual(
                    {"default", "minimum"},
                    set(recipe.form["handheld"]["main"]),
                )
                self.assertEqual(
                    {"default", "minimum"},
                    set(recipe.form["handheld"]["detail"]),
                )

    def test_contract_requires_both_count_fields(self) -> None:
        contract = json.loads(
            (ROOT / "categories" / "_shared" / "batch-intake-contract.json").read_text(
                encoding="utf-8"
            )
        )
        facts = contract["payload"]["properties"]["facts"]
        self.assertIn("main_image_count", facts["required"])
        self.assertIn("detail_image_count", facts["required"])
        self.assertEqual({"type": "integer"}, facts["properties"]["main_image_count"])
        self.assertEqual({"type": "integer"}, facts["properties"]["detail_image_count"])


class ManifestCompatibilityTests(unittest.TestCase):
    def test_old_category_manifest_uses_recipe_defaults_without_writeback(self) -> None:
        manifest = {"category": "杯类", "user_confirmed_facts": dict(OLD_CATEGORY_FACTS)}
        before = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        parsed = parse_user_confirmed_requirements(manifest, ROOT)
        self.assertEqual((6, 8), (parsed.main_image_count, parsed.detail_image_count))
        self.assertEqual(before, json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    def test_legacy_manifest_without_category_uses_cup_recipe_defaults(self) -> None:
        parsed = parse_user_confirmed_requirements(
            {"user_confirmed_facts": dict(LEGACY_FACTS)},
            ROOT,
        )
        self.assertEqual("杯类", parsed.category)
        self.assertEqual((6, 8), (parsed.main_image_count, parsed.detail_image_count))

    def test_new_manifest_uses_its_per_batch_counts(self) -> None:
        facts = {
            **OLD_CATEGORY_FACTS,
            "main_image_count": 3,
            "detail_image_count": 2,
            "handheld_main": 3,
            "handheld_detail": 2,
        }
        parsed = parse_user_confirmed_requirements(
            {"category": "杯类", "user_confirmed_facts": facts},
            ROOT,
        )
        self.assertEqual((3, 2), (parsed.main_image_count, parsed.detail_image_count))
        self.assertEqual((3, 2), (parsed.handheld_main, parsed.handheld_detail))

    def test_new_manifest_rejects_bad_counts_and_handheld_overflow(self) -> None:
        cases = (
            {"main_image_count": 0, "detail_image_count": 8},
            {"main_image_count": 31, "detail_image_count": 8},
            {"main_image_count": -1, "detail_image_count": 8},
            {"main_image_count": 1.5, "detail_image_count": 8},
            {"main_image_count": "6", "detail_image_count": 8},
            {"main_image_count": True, "detail_image_count": 8},
            {
                "main_image_count": 2,
                "detail_image_count": 8,
                "handheld_main": 3,
            },
            {
                "main_image_count": 6,
                "detail_image_count": 1,
                "handheld_detail": 2,
            },
        )
        for patch in cases:
            facts = {
                **OLD_CATEGORY_FACTS,
                "main_image_count": 6,
                "detail_image_count": 8,
                **patch,
            }
            with self.subTest(patch=patch):
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(
                        {"category": "杯类", "user_confirmed_facts": facts},
                        ROOT,
                    )

    def test_partially_added_count_fields_do_not_fall_back(self) -> None:
        facts = {**OLD_CATEGORY_FACTS, "main_image_count": 6}
        with self.assertRaises(ExecutorExecutionError):
            parse_user_confirmed_requirements(
                {"category": "杯类", "user_confirmed_facts": facts},
                ROOT,
            )


class ManifestCliTests(unittest.TestCase):
    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_batch_manifest.py"),
                "--product-id",
                "cnt01_cli_dry_run",
                "--product-type",
                "杯子",
                "--height-cm",
                "25",
                "--handheld-main",
                "1",
                "--handheld-detail",
                "1",
                "--allow-clear-water",
                "true",
                "--forbid-pouring-and-heating",
                "true",
                "--missing-d-no-retake",
                "true",
                "--dry-run",
                *extra,
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_old_cli_call_uses_recipe_defaults(self) -> None:
        completed = self.run_cli()
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        facts = json.loads(completed.stdout)["manifest_data"]["user_confirmed_facts"]
        self.assertEqual((6, 8), (facts["main_image_count"], facts["detail_image_count"]))

    def test_cli_accepts_both_boundaries(self) -> None:
        for count in (1, 30):
            with self.subTest(count=count):
                completed = self.run_cli(
                    "--main-count",
                    str(count),
                    "--detail-count",
                    str(count),
                )
                self.assertEqual(
                    0,
                    completed.returncode,
                    completed.stderr or completed.stdout,
                )
                facts = json.loads(completed.stdout)["manifest_data"][
                    "user_confirmed_facts"
                ]
                self.assertEqual(
                    (count, count),
                    (facts["main_image_count"], facts["detail_image_count"]),
                )

    def test_cli_rejects_out_of_range_counts_and_handheld_overflow(self) -> None:
        cases = (
            ("--main-count", "0"),
            ("--detail-count", "31"),
            ("--main-count", "-1"),
            ("--main-count", "1", "--handheld-main", "2"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(2, self.run_cli(*arguments).returncode)


if __name__ == "__main__":
    unittest.main()
