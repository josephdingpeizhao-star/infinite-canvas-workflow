from __future__ import annotations

import copy
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for extra in (BRIDGE, SCRIPTS, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from final_prompt_integrity_fixtures import (  # noqa: E402
    build_final_prompt_bundle,
    read_json,
    write_json,
)
from final_prompt_literal_contract import (  # noqa: E402
    has_required_canvas_ratio_literal,
    has_required_confirmed_height_literal,
    required_canvas_ratio_literal,
    required_confirmed_height_literal,
)
from category_recipes import (  # noqa: E402
    installed_category_metadata,
    load_category_recipe,
)
from codex_dev_downstream import (  # noqa: E402
    UserConfirmedRequirements,
    parse_final_prompt_batch_response,
    stable_json_sha256,
)
from executor_contract import ExecutorExecutionError  # noqa: E402
from validate_final_prompt_integrity import build_prompts_only_report  # noqa: E402


FIXED_FORMS = ("固定为", "固定", "")
RATIO_SEPARATORS = (":", "：", "、")
RATIO_SPACING = (" ", "")
HEIGHT_UNITS = ("厘米", "cm", "CM")
HEIGHT_PREFIXES = ("高度约", "高约", "整壶约", "通体约", "产品高度约")
HEIGHT_VALUES = ("25", "25.5")
MODES = ("main", "detail")
MATRIX_SIZE = (
    len(FIXED_FORMS)
    * len(RATIO_SEPARATORS)
    * len(RATIO_SPACING)
    * len(HEIGHT_UNITS)
    * len(HEIGHT_PREFIXES)
    * len(HEIGHT_VALUES)
    * len(MODES)
)


class FinalPromptLiteralContractTest(unittest.TestCase):
    @staticmethod
    def _dynamic_parser_inputs(
        *,
        category_key: str,
        mode: str,
    ) -> tuple[
        UserConfirmedRequirements,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        recipe = load_category_recipe(ROOT, category_key)
        required_dimensions = set(recipe.form["dimensions"]["required"])
        requirements = UserConfirmedRequirements(
            product_type=recipe.product_noun,
            height_cm=25,
            handheld_main=0,
            handheld_detail=0,
            allow_clear_water=False,
            forbid_pouring_and_heating=True,
            missing_d_no_retake=True,
            main_image_count=1,
            detail_image_count=1,
            length_cm=30 if "length_cm" in required_dimensions else None,
            width_cm=20 if "width_cm" in required_dimensions else None,
            category=category_key,
            recipe=recipe,
        )
        config_id = f"{mode}_01"
        common: dict[str, object] = {}
        overrides = {
            "绑定角度槽位": "A 槽位，绑定源图 img_001；本张仅调用这一张白底图。",
            "手持交互声明": "本张图不启用手持场景",
        }
        resolved = dict(common)
        resolved.update(overrides)
        variable_config = {
            "product_id": "dynamic_category",
            "artifact_type": f"{mode}_variable_config",
            "config_count": 1,
            "upstream_artifacts": {},
            "common_constraints": common,
            "configs": [
                {
                    "config_id": config_id,
                    "output_type": mode,
                    "per_image_overrides": overrides,
                    "resolved_variable_config_sha256": stable_json_sha256(resolved),
                    "notes": "",
                }
            ],
            "notes": "",
        }
        angle_inventory = {
            "angle_slots": [
                {
                    "source_asset_id": "img_001",
                    "angle_slot": "A",
                    "admission_result": "合格，可进入对应槽位",
                }
            ],
            "missing_angle_slots": ["D"],
        }
        ratio = "1:1" if mode == "main" else "3:4"
        response = {
            "prompts": [
                {
                    "config_id": config_id,
                    "final_prompt": (
                        f"画布比例固定为 {ratio}；绑定源图 img_001，A 槽位；"
                        "高度约 25 厘米；本张图不启用手持场景。"
                    ),
                    "negative_prompt": "不出现任何禁止的内容物或动作",
                }
            ]
        }
        return requirements, angle_inventory, variable_config, response

    def test_compiler_contract_is_never_looser_than_real_gate_generated_matrix(self) -> None:
        violations: list[str] = []
        actual_cases = 0
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            base_documents = {
                mode: read_json(bundle.prompt_path(f"{mode}_01"))
                for mode in MODES
            }

            for (
                fixed_form,
                ratio_separator,
                ratio_spacing,
                height_unit,
                height_prefix,
                height_value,
                mode,
            ) in itertools.product(
                FIXED_FORMS,
                RATIO_SEPARATORS,
                RATIO_SPACING,
                HEIGHT_UNITS,
                HEIGHT_PREFIXES,
                HEIGHT_VALUES,
                MODES,
            ):
                actual_cases += 1
                ratio = "1:1" if mode == "main" else "3:4"
                ratio_value = ratio.replace(":", ratio_separator)
                ratio_literal = f"画布比例{fixed_form}{ratio_spacing}{ratio_value}"
                height_literal = f"{height_prefix} {height_value} {height_unit}"
                compiler_accepts = (
                    has_required_canvas_ratio_literal(ratio_literal, ratio)
                    and has_required_confirmed_height_literal(height_literal, 25)
                )

                document = copy.deepcopy(base_documents[mode])
                original = document["final_prompt"]
                self.assertIn(f"画布比例固定为 {ratio}", original)
                self.assertIn("产品高度约 25 厘米", original)
                document["final_prompt"] = original.replace(
                    f"画布比例固定为 {ratio}",
                    ratio_literal,
                    1,
                ).replace(
                    "产品高度约 25 厘米",
                    height_literal,
                    1,
                )
                write_json(bundle.prompt_path(f"{mode}_01"), document)
                report = build_prompts_only_report(
                    batch_manifest_path=bundle.manifest_path
                )
                issue_ids = {item["issue_id"] for item in report["issues"]}
                gate_accepts = not {
                    f"canvas_ratio_literal_mismatch_{mode}_01",
                    f"confirmed_height_literal_mismatch_{mode}_01",
                }.intersection(issue_ids)

                if compiler_accepts and not gate_accepts:
                    violations.append(
                        " / ".join(
                            (
                                f"mode={mode}",
                                f"ratio={ratio_literal!r}",
                                f"height={height_literal!r}",
                                f"issues={sorted(issue_ids)!r}",
                            )
                        )
                    )

        self.assertEqual(1080, MATRIX_SIZE)
        self.assertEqual(MATRIX_SIZE, actual_cases)
        self.assertFalse(
            violations,
            "compiler accepted forms rejected by the independent gate "
            f"({len(violations)}/{actual_cases}):\n" + "\n".join(violations[:20]),
        )

    def test_canonical_literals_pass_compiler_and_real_gate_for_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            for mode, ratio in (("main", "1:1"), ("detail", "3:4")):
                with self.subTest(mode=mode):
                    ratio_literal = required_canvas_ratio_literal(ratio)
                    height_literal = required_confirmed_height_literal(25)
                    self.assertTrue(
                        has_required_canvas_ratio_literal(ratio_literal, ratio)
                    )
                    self.assertTrue(
                        has_required_confirmed_height_literal(height_literal, 25)
                    )

                    prompt_path = bundle.prompt_path(f"{mode}_01")
                    document = read_json(prompt_path)
                    document["final_prompt"] = document["final_prompt"].replace(
                        f"画布比例固定为 {ratio}",
                        ratio_literal,
                        1,
                    ).replace(
                        "产品高度约 25 厘米",
                        height_literal,
                        1,
                    )
                    write_json(prompt_path, document)

            report = build_prompts_only_report(
                batch_manifest_path=bundle.manifest_path
            )
            issue_ids = {item["issue_id"] for item in report["issues"]}
            for mode in MODES:
                self.assertNotIn(
                    f"canvas_ratio_literal_mismatch_{mode}_01",
                    issue_ids,
                )
                self.assertNotIn(
                    f"confirmed_height_literal_mismatch_{mode}_01",
                    issue_ids,
                )

    def test_paraphrases_are_rejected_at_compile_time(self) -> None:
        self.assertFalse(
            has_required_canvas_ratio_literal("输出画布比例：1:1", "1:1")
        )
        self.assertFalse(
            has_required_confirmed_height_literal("高约 25 厘米", 25)
        )
        self.assertTrue(
            has_required_canvas_ratio_literal("画布比例固定为1:1", "1:1")
        )
        self.assertTrue(
            has_required_confirmed_height_literal("高度 约 25 cm", 25)
        )

    def test_decimal_height_is_rejected_by_compiler_and_gate_input_contracts(self) -> None:
        with self.assertRaises(ValueError):
            required_confirmed_height_literal(25.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            has_required_confirmed_height_literal(
                "高度约 25.5 厘米",
                25.5,  # type: ignore[arg-type]
            )

        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            manifest = read_json(bundle.manifest_path)
            manifest["user_confirmed_facts"] = {
                "product_type": "水壶",
                "height_cm": 25.5,
                "handheld_main": 2,
                "handheld_detail": 1,
                "allow_clear_water": False,
                "forbid_pouring_and_heating": True,
                "missing_d_no_retake": True,
            }
            write_json(bundle.manifest_path, manifest)
            report = build_prompts_only_report(
                batch_manifest_path=bundle.manifest_path
            )
            issue_ids = {item["issue_id"] for item in report["issues"]}
            self.assertIn("user_confirmed_facts_invalid", issue_ids)

    def test_real_parser_covers_every_installed_category_and_both_modes(self) -> None:
        metadata = installed_category_metadata(ROOT)
        self.assertTrue(metadata)
        actual_cases = 0
        for category in metadata:
            category_key = str(category["key"])
            for mode in MODES:
                actual_cases += 1
                with self.subTest(category=category_key, mode=mode, form="canonical"):
                    (
                        requirements,
                        angle_inventory,
                        variable_config,
                        response,
                    ) = self._dynamic_parser_inputs(
                        category_key=category_key,
                        mode=mode,
                    )
                    parsed = parse_final_prompt_batch_response(
                        json.dumps(response, ensure_ascii=False),
                        mode=mode,
                        product_id="dynamic_category",
                        requirements=requirements,
                        angle_inventory=angle_inventory,
                        variable_config=variable_config,
                    )
                    self.assertEqual([f"{mode}_01"], list(parsed))

                ratio = "1:1" if mode == "main" else "3:4"
                ratio_paraphrase = copy.deepcopy(response)
                ratio_paraphrase["prompts"][0]["final_prompt"] = ratio_paraphrase[
                    "prompts"
                ][0]["final_prompt"].replace(
                    f"画布比例固定为 {ratio}",
                    f"输出画布比例：{ratio}",
                )
                with self.subTest(category=category_key, mode=mode, form="ratio"):
                    with self.assertRaisesRegex(
                        ExecutorExecutionError,
                        "未保留画布比例",
                    ):
                        parse_final_prompt_batch_response(
                            json.dumps(
                                ratio_paraphrase,
                                ensure_ascii=False,
                            ),
                            mode=mode,
                            product_id="dynamic_category",
                            requirements=requirements,
                            angle_inventory=angle_inventory,
                            variable_config=variable_config,
                        )

                height_paraphrase = copy.deepcopy(response)
                height_paraphrase["prompts"][0]["final_prompt"] = height_paraphrase[
                    "prompts"
                ][0]["final_prompt"].replace(
                    "高度约 25 厘米",
                    "高约 25 厘米",
                )
                with self.subTest(category=category_key, mode=mode, form="height"):
                    with self.assertRaisesRegex(
                        ExecutorExecutionError,
                        "未保留已确认高度",
                    ):
                        parse_final_prompt_batch_response(
                            json.dumps(
                                height_paraphrase,
                                ensure_ascii=False,
                            ),
                            mode=mode,
                            product_id="dynamic_category",
                            requirements=requirements,
                            angle_inventory=angle_inventory,
                            variable_config=variable_config,
                        )

        self.assertEqual(len(metadata) * len(MODES), actual_cases)


if __name__ == "__main__":
    unittest.main()
