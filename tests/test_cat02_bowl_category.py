from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import BatchCreator, UploadedFile, prepare_state_root  # noqa: E402
from batch_intake_contract import batch_intake_contract_sha256  # noqa: E402
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from category_recipes import (  # noqa: E402
    DEFAULT_CATEGORY_KEY,
    installed_category_metadata,
    load_category_recipe,
)
from codex_dev_downstream import (  # noqa: E402
    ExecutorExecutionError,
    build_variable_config_prompt,
    parse_user_confirmed_requirements,
)
from workflow_production_http_server import (  # noqa: E402
    WorkflowProductionHttpApplication,
)


EXPECTED_CONTRACT_HASH = (
    "a030df8d0aa9c96d9275d7c6f463fbc9d8f10af57e8c4539c2cb9d0d903456d3"
)
EXPECTED_BOWL_FILES = (
    "form.json",
    "lexicons.json",
    "prompts/angle.md",
    "prompts/angle_boundary.md",
    "prompts/detail.md",
    "prompts/final.md",
    "prompts/identity.md",
    "prompts/main.md",
    "prompts/style.md",
    "qc/checklist.md",
    "qc/realism.md",
    "qc/runtime.json",
    "qc/workflow.md",
    "recipe.json",
    "runtime/detail.json",
    "runtime/final.json",
    "runtime/main.json",
)
BOWL_FACTS = {
    "product_type": "碗",
    "length_cm": 18,
    "width_cm": None,
    "height_cm": 8,
    "main_image_count": 6,
    "detail_image_count": 8,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": False,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}


def _frontend_integer_bounds(
    minimum: object,
    maximum: object,
    default: object | None = None,
    *,
    floor: int = 0,
) -> bool:
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or minimum < floor
        or maximum < minimum
    ):
        return False
    return default is None or (
        type(default) is int and minimum <= default <= maximum
    )


def _frontend_valid_category(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(
        not isinstance(value.get(key), str) or not value[key]
        for key in ("key", "display_name", "product_noun")
    ):
        return False
    form = value.get("form")
    if not isinstance(form, Mapping):
        return False
    dimensions = form.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return False
    required = dimensions.get("required")
    fields = dimensions.get("fields")
    dimension_keys = {"length_cm", "width_cm", "height_cm"}
    if (
        not isinstance(required, list)
        or not isinstance(fields, list)
        or len(fields) != 3
        or any(item not in dimension_keys for item in required)
    ):
        return False
    if (
        len({item.get("key") for item in fields if isinstance(item, Mapping)}) != 3
        or any(
            not isinstance(item, Mapping)
            or item.get("key") not in dimension_keys
            or not item.get("label")
            or not item.get("unit")
            or not _frontend_integer_bounds(
                item.get("minimum"),
                item.get("maximum"),
            )
            for item in fields
        )
    ):
        return False
    image_counts = form.get("image_counts")
    if not isinstance(image_counts, Mapping):
        return False
    for mode in ("main", "detail"):
        spec = image_counts.get(mode)
        if not isinstance(spec, Mapping) or not _frontend_integer_bounds(
            spec.get("minimum"),
            spec.get("maximum"),
            spec.get("default"),
            floor=1,
        ):
            return False
    handheld = form.get("handheld")
    if not isinstance(handheld, Mapping):
        return False
    for mode in ("main", "detail"):
        spec = handheld.get(mode)
        if (
            not isinstance(spec, Mapping)
            or type(spec.get("minimum")) is not int
            or spec["minimum"] < 0
            or type(spec.get("default")) is not int
            or spec["default"] < spec["minimum"]
        ):
            return False
    advanced = form.get("advanced_options")
    expected_advanced = {
        "forbid_pouring_and_heating",
        "missing_d_no_retake",
    }
    return (
        isinstance(advanced, list)
        and len(advanced) == 2
        and {
            item.get("field")
            for item in advanced
            if isinstance(item, Mapping)
        }
        == expected_advanced
        and all(
            isinstance(item, Mapping)
            and item.get("field") in expected_advanced
            and type(item.get("default")) is bool
            and bool(item.get("label"))
            and bool(item.get("description"))
            for item in advanced
        )
    )


class Cat02BowlCategoryTest(unittest.TestCase):
    def test_bowl_recipe_matches_authoritative_schema_and_file_manifest(self) -> None:
        bowl_root = ROOT / "categories" / "碗"
        actual_files = tuple(
            sorted(
                path.relative_to(bowl_root).as_posix()
                for path in bowl_root.rglob("*")
                if path.is_file()
            )
        )
        self.assertEqual(EXPECTED_BOWL_FILES, actual_files)

        schema = json.loads(
            (
                ROOT
                / "categories"
                / "_shared"
                / "category-recipe.schema.json"
            ).read_text(encoding="utf-8")
        )
        recipe = json.loads(
            (bowl_root / "recipe.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(recipe))
        self.assertEqual(1, recipe["schema_version"])
        self.assertEqual("碗", recipe["key"])
        self.assertEqual("碗", recipe["display_name"])
        self.assertEqual("碗", recipe["product_noun"])
        self.assertEqual(
            "approved",
            recipe["business_review_status"],
        )
        self.assertEqual(
            set(EXPECTED_BOWL_FILES) - {"recipe.json"},
            set(recipe["files"].values()),
        )
        for relative in (
            "runtime/main.json",
            "runtime/detail.json",
            "runtime/final.json",
            "qc/runtime.json",
        ):
            package = json.loads(
                (bowl_root / relative).read_text(encoding="utf-8")
            )
            for rule_slice in package["slices"]:
                source = rule_slice["source_file"]
                with self.subTest(relative=relative, source=source):
                    self.assertTrue(source.startswith("categories/碗/"))
                    source_path = ROOT / source
                    self.assertTrue(source_path.is_file())
                    line_count = len(source_path.read_text(encoding="utf-8").splitlines())
                    self.assertGreaterEqual(rule_slice["line_start"], 1)
                    self.assertLessEqual(rule_slice["line_end"], line_count)

    def test_bowl_is_discovered_in_order_without_changing_cup_default(self) -> None:
        metadata = installed_category_metadata(ROOT)

        self.assertEqual(
            ["杯类", "盘子", "碗"],
            [item["key"] for item in metadata],
        )
        self.assertEqual("杯类", DEFAULT_CATEGORY_KEY)
        bowl = metadata[-1]
        self.assertEqual(("碗", "碗"), (bowl["display_name"], bowl["product_noun"]))

    def test_bowl_form_has_exact_fields_bounds_defaults_and_human_copy(self) -> None:
        form = load_category_recipe(ROOT, "碗").form

        self.assertEqual(
            ["length_cm", "height_cm"],
            form["dimensions"]["required"],
        )
        self.assertEqual(
            [
                {
                    "key": "length_cm",
                    "label": "口径",
                    "unit": "厘米",
                    "minimum": 1,
                    "maximum": 9999,
                },
                {
                    "key": "width_cm",
                    "label": "宽",
                    "unit": "厘米",
                    "minimum": 1,
                    "maximum": 9999,
                },
                {
                    "key": "height_cm",
                    "label": "高",
                    "unit": "厘米",
                    "minimum": 1,
                    "maximum": 9999,
                },
            ],
            form["dimensions"]["fields"],
        )
        self.assertEqual(
            {
                "main": {"default": 6, "minimum": 1, "maximum": 30},
                "detail": {"default": 8, "minimum": 1, "maximum": 30},
            },
            form["image_counts"],
        )
        self.assertEqual(
            {
                "main": {"default": 2, "minimum": 0},
                "detail": {"default": 1, "minimum": 0},
            },
            form["handheld"],
        )
        self.assertEqual(
            [
                (
                    "forbid_pouring_and_heating",
                    True,
                    "不出现倾倒、倒水、加热等动作画面",
                ),
                (
                    "missing_d_no_retake",
                    True,
                    "拍摄角度不全时直接继续",
                ),
            ],
            [
                (item["field"], item["default"], item["label"])
                for item in form["advanced_options"]
            ],
        )
        self.assertTrue(
            all(item["description"].strip() for item in form["advanced_options"])
        )

    def test_bowl_full_recipe_chain_loads_and_all_templates_render(self) -> None:
        recipe = load_category_recipe(ROOT, "碗")
        self.assertEqual(
            {
                "identity_prompt",
                "style_prompt",
                "angle_prompt",
                "angle_boundary",
                "main_prompt",
                "detail_prompt",
                "final_prompt",
            },
            set(recipe.prompts),
        )
        self.assertEqual(
            {"main_runtime", "detail_runtime", "final_runtime", "qc_runtime"},
            set(recipe.runtime_packages),
        )
        self.assertEqual(
            {"qc_checklist", "qc_workflow", "qc_realism"},
            set(recipe.qc_documents),
        )
        values = {
            "length_cm": 18,
            "width_cm": None,
            "height_cm": 8,
            "expected_ratio": "1:1",
            "handheld_phrase": recipe.lexicons["handheld_phrase"],
            "handheld_count_text": "恰好 2 项",
            "product_material_term_rule": "只使用已确认商品事实。",
            "scene_safety_collective_rule": "不出现任何禁止的内容物或动作。",
            "scene_rule": "只允许空置。",
            "optional_dimensions_main": "",
            "optional_dimensions_detail": "",
            "optional_dimensions_final": "",
        }
        for name in recipe.prompts:
            with self.subTest(name=name):
                self.assertTrue(recipe.render_prompt(name, **values).strip())

        requirements = parse_user_confirmed_requirements(
            {"category": "碗", "user_confirmed_facts": BOWL_FACTS},
            ROOT,
        )
        prompt = build_variable_config_prompt(
            mode="main",
            product_id="bowl_fixture",
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
        self.assertIn("口径约 18 厘米、高度约 8 厘米", prompt)
        self.assertIn("整组视为同一个商品单元", prompt)
        self.assertIn("bowl-main-identity-angle-and-combo", prompt)
        self.assertNotIn("None", prompt)

        optional_width = parse_user_confirmed_requirements(
            {
                "category": "碗",
                "user_confirmed_facts": BOWL_FACTS | {"width_cm": 16},
            },
            ROOT,
        )
        prompt_with_width = build_variable_config_prompt(
            mode="main",
            product_id="bowl_optional_width",
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
            requirements=optional_width,
        )
        self.assertIn("宽约 16 厘米", prompt_with_width)

    def test_bowl_required_optional_dimensions_and_bounds_are_enforced(self) -> None:
        minimum = {
            **BOWL_FACTS,
            "length_cm": 1,
            "width_cm": None,
            "height_cm": 1,
        }
        parsed = parse_user_confirmed_requirements(
            {"category": "碗", "user_confirmed_facts": minimum},
            ROOT,
        )
        self.assertEqual((1, None, 1), (parsed.length_cm, parsed.width_cm, parsed.height_cm))

        maximum = {
            **BOWL_FACTS,
            "length_cm": 9999,
            "width_cm": 9999,
            "height_cm": 9999,
        }
        parsed = parse_user_confirmed_requirements(
            {"category": "碗", "user_confirmed_facts": maximum},
            ROOT,
        )
        self.assertEqual(
            (9999, 9999, 9999),
            (parsed.length_cm, parsed.width_cm, parsed.height_cm),
        )

        for field, value in (
            ("length_cm", None),
            ("height_cm", None),
            ("length_cm", 0),
            ("height_cm", 10000),
            ("width_cm", 0),
            ("width_cm", 10000),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(
                        {
                            "category": "碗",
                            "user_confirmed_facts": {
                                **BOWL_FACTS,
                                field: value,
                            },
                        },
                        ROOT,
                    )

    def test_bowl_count_and_handheld_boundaries_follow_cnt01(self) -> None:
        for count in (1, 30):
            with self.subTest(count=count):
                detail_handheld = count - 1
                parsed = parse_user_confirmed_requirements(
                    {
                        "category": "碗",
                        "user_confirmed_facts": {
                            **BOWL_FACTS,
                            "main_image_count": count,
                            "detail_image_count": count,
                            "handheld_main": count,
                            "handheld_detail": detail_handheld,
                        },
                    },
                    ROOT,
                )
                self.assertEqual(
                    (count, count, count, detail_handheld),
                    (
                        parsed.main_image_count,
                        parsed.detail_image_count,
                        parsed.handheld_main,
                        parsed.handheld_detail,
                    ),
                )

        for patch in (
            {"main_image_count": 0},
            {"detail_image_count": 31},
            {"main_image_count": 1, "handheld_main": 2},
            {"detail_image_count": 1, "handheld_detail": 2},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(
                        {
                            "category": "碗",
                            "user_confirmed_facts": BOWL_FACTS | patch,
                        },
                        ROOT,
                    )

    def test_batch_categories_contains_bowl_and_keeps_exact_hash_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            shutil.copytree(ROOT / "categories", repository / "categories")
            before = {
                path.relative_to(repository).as_posix(): path.read_bytes()
                for path in repository.rglob("*")
                if path.is_file()
            }
            application = WorkflowProductionHttpApplication(
                repository,
                "cat02-test-auth",
            )

            payload = application.batch_categories()

            self.assertEqual(True, payload["ok"])
            self.assertEqual(EXPECTED_CONTRACT_HASH, payload["contractHash"])
            self.assertEqual(
                ["杯类", "盘子", "碗"],
                [item["key"] for item in payload["categories"]],
            )
            self.assertEqual(
                EXPECTED_CONTRACT_HASH,
                batch_intake_contract_sha256(repository),
            )
            after = {
                path.relative_to(repository).as_posix(): path.read_bytes()
                for path in repository.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_all_installed_categories_pass_frontend_isomorphic_validation(self) -> None:
        metadata = installed_category_metadata(ROOT)

        self.assertEqual(len(metadata), len({item["key"] for item in metadata}))
        for category in metadata:
            with self.subTest(category=category["key"]):
                self.assertTrue(_frontend_valid_category(category))

    def test_combo_unit_terms_exist_and_cross_category_residue_is_absent(self) -> None:
        bowl_root = ROOT / "categories" / "碗"
        required_documents = (
            "prompts/identity.md",
            "prompts/main.md",
            "prompts/detail.md",
            "prompts/final.md",
            "qc/checklist.md",
        )
        required_phrases = (
            "整组视为同一个商品单元",
            "组件种类、数量和搭配关系",
            "碗数和配套盘数",
            "不一致即判定不合格并返修",
        )
        for relative in required_documents:
            text = (bowl_root / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                for phrase in required_phrases:
                    self.assertIn(phrase, text)

        lexicons = json.loads(
            (bowl_root / "lexicons.json").read_text(encoding="utf-8")
        )
        serialized_lexicons = json.dumps(lexicons, ensure_ascii=False)
        for phrase in ("配套盘", "碗盘成套", "多碗成套", "叠放", "垫盘"):
            self.assertIn(phrase, serialized_lexicons)

        all_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in bowl_root.rglob("*")
            if path.is_file()
        )
        for forbidden in (
            "盘子专用",
            "盘子品类",
            "你负责为单个盘子",
            "盘面主图案",
            "托住盘底",
            "plate-",
            "CAT-01 盘子",
            "杯身",
            "杯口",
            "杯沿",
            "杯柄",
            "壶身",
            "壶口",
            "壶嘴",
            "壶盖",
            "提梁",
            "水壶类",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, all_text)

    def test_bowl_batch_creation_preserves_eleven_facts_and_contract_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repository = base / "repo"
            test_root = base / "isolated-test-root"
            state_root = base / "state"
            upload_root = base / "uploads"
            (repository / "scripts").mkdir(parents=True)
            (repository / "manifests").mkdir()
            (repository / "canvas-bridge").mkdir()
            test_root.mkdir()
            upload_root.mkdir()
            (test_root / ".canvas_intake_test_root").write_text(
                "canvas-intake-test-root-v1\n",
                encoding="utf-8",
            )
            prepare_state_root(state_root)
            shutil.copy2(
                ROOT / "scripts" / "build_batch_manifest.py",
                repository / "scripts",
            )
            for name in ("category_recipes.py", "image_count_contract.py"):
                shutil.copy2(
                    ROOT / "canvas-bridge" / name,
                    repository / "canvas-bridge",
                )
            shutil.copytree(ROOT / "categories", repository / "categories")
            for name in (
                "batch_manifest.template.json",
                "asset_manifest.template.json",
            ):
                shutil.copy2(
                    ROOT / "manifests" / name,
                    repository / "manifests",
                )

            facts = ConfirmedFacts(
                product_type="碗",
                length_cm=18,
                width_cm=16,
                height_cm=8,
                main_image_count=3,
                detail_image_count=2,
                handheld_main=2,
                handheld_detail=1,
                forbid_pouring_and_heating=True,
                missing_d_no_retake=True,
            )
            payload = b"cat02-bowl-original\x00\x01"
            source = SourceImage(
                node_id="image-1",
                storage_key="image:bowl",
                name="碗正面.png",
                size=len(payload),
                mime_type="image/png",
                last_modified=1_722_112_000_000,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            request = BatchIntakeRequest(
                request_id="cat02-bowl-request",
                requested_at=19_000,
                info_node_id="info-1",
                workflow_node_id="workflow-1",
                facts=facts,
                source_images=(source,),
                category="碗",
                contract_hash=EXPECTED_CONTRACT_HASH,
            )
            uploaded_path = upload_root / "upload.bin"
            uploaded_path.write_bytes(payload)
            upload = UploadedFile(
                source_node_id=source.node_id,
                path=uploaded_path,
                name=source.name,
                size=len(payload),
                mime_type=source.mime_type,
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            creator = BatchCreator(
                repo_root=repository,
                state_root=state_root,
                test_root=test_root,
                now=lambda: datetime(2026, 7, 28, 12, 34, 56),
            )

            result = creator.create(request, [upload])
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))

            self.assertEqual("碗_20260728_123456", result.product_id)
            self.assertEqual("碗", manifest["category"])
            self.assertEqual(10, len(manifest["user_confirmed_facts"]))
            self.assertEqual(facts.as_dict(), manifest["user_confirmed_facts"])
            self.assertEqual("碗", receipt["category"])
            self.assertEqual(EXPECTED_CONTRACT_HASH, receipt["contract_hash"])
            self.assertEqual(
                payload,
                (
                    result.workspace_root
                    / "inputs"
                    / "white_bg"
                    / "碗正面.png"
                ).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
