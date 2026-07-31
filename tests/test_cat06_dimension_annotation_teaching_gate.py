from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import load_category_recipe  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    _category_prompt_values,
    parse_user_confirmed_requirements,
)


_PREFERRED_DIMENSIONS = {
    "length_cm": 18,
    "width_cm": 16,
    "height_cm": 8,
}
# Both detail-chunk prevalidation and assembled-detail revalidation enforce
# this same teaching-facing literal contract.
_UNCONFIRMED_PARAMETER_TERMS = ("容量", "宽度", "直径", "重量", "材质")
_CLAUSE_BOUNDARY = re.compile(r"[。！？!?；;]")
_PROHIBITION_MARKER = re.compile(r"(?<!不)禁止")
_DUAL_HEIGHT_TEMPLATE = re.compile(
    r"【尺寸标注信息】与【尺寸标注图规则】(?:都|均)必须包含"
    r"[“\"]高度约\s*\{height_cm\}\s*厘米[”\"]"
)


@dataclass(frozen=True)
class _CompiledDetailTeaching:
    category_key: str
    height_literal: str
    rendered_prompt: str
    runtime_text: str

    @property
    def combined_text(self) -> str:
        return f"{self.rendered_prompt}\n{self.runtime_text}"


def _installed_category_keys(root: Path) -> tuple[str, ...]:
    keys = tuple(
        path.name
        for path in sorted((root / "categories").iterdir(), key=lambda item: item.name)
        if path.is_dir()
        and not path.name.startswith("_")
        and (path / "recipe.json").is_file()
    )
    if not keys:
        raise AssertionError("CAT-06 did not discover any installed category")
    return keys


def _legal_dimension_value(field: dict[str, Any]) -> int:
    key = str(field["key"])
    preferred = _PREFERRED_DIMENSIONS[key]
    minimum = int(field["minimum"])
    maximum = int(field["maximum"])
    return min(max(preferred, minimum), maximum)


def _compile_detail_teaching(root: Path, category_key: str) -> _CompiledDetailTeaching:
    recipe = load_category_recipe(root, category_key)
    dimensions = {
        str(field["key"]): _legal_dimension_value(field)
        for field in recipe.form["dimensions"]["fields"]
    }
    facts: dict[str, Any] = {
        "product_type": recipe.product_noun,
        **dimensions,
        "handheld_main": int(recipe.form["handheld"]["main"]["default"]),
        "handheld_detail": int(recipe.form["handheld"]["detail"]["default"]),
    }
    facts.update(
        {
            str(option["field"]): option["default"]
            for option in recipe.form["advanced_options"]
        }
    )
    requirements = parse_user_confirmed_requirements(
        {
            "category": category_key,
            "user_confirmed_facts": facts,
        },
        root,
    )
    rendered_prompt = recipe.render_prompt(
        "detail_prompt",
        **_category_prompt_values(
            requirements,
            handheld_count=requirements.handheld_detail,
        ),
    )

    package = recipe.runtime_packages["detail_runtime"]
    slices = package.get("slices")
    if not isinstance(slices, list):
        raise AssertionError(f"CAT-06 malformed detail runtime: {category_key}")
    runtime_parts: list[str] = []
    for item in slices:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise AssertionError(f"CAT-06 malformed detail runtime slice: {category_key}")
        runtime_parts.append(item["text"])

    return _CompiledDetailTeaching(
        category_key=category_key,
        height_literal=f"高度约 {requirements.height_cm} 厘米",
        rendered_prompt=rendered_prompt,
        runtime_text="\n".join(runtime_parts),
    )


def _teaching_units(text: str) -> tuple[str, ...]:
    return tuple(
        " ".join(line.split())
        for line in text.splitlines()
        if line.strip()
    )


def _flexible_literal_pattern(literal: str) -> str:
    return r"\s*".join(re.escape(part) for part in literal.split())


def _has_dual_height_contract(
    unit: str,
    *,
    rendered_height_literal: str,
) -> bool:
    height_pattern = (
        rf"(?:{_flexible_literal_pattern(rendered_height_literal)}|"
        r"高度约\s*\{height_cm\}\s*厘米)"
    )
    contract_pattern = re.compile(
        r"【尺寸标注信息】\s*与\s*【尺寸标注图规则】\s*"
        r"(?:都|均)必须包含\s*[“\"]?"
        + height_pattern
        + r"[”\"]?"
    )
    return bool(contract_pattern.search(unit))


def _has_size_info_prohibition_contract(unit: str) -> bool:
    size_info_index = unit.find("【尺寸标注信息】")
    if size_info_index < 0:
        return False

    search_start = size_info_index + len("【尺寸标注信息】")
    for match in _PROHIBITION_MARKER.finditer(unit, search_start):
        clause = _CLAUSE_BOUNDARY.split(unit[match.start() :], maxsplit=1)[0]
        if all(term in clause for term in _UNCONFIRMED_PARAMETER_TERMS):
            return True
    return False


def _missing_contracts(teaching: _CompiledDetailTeaching) -> tuple[str, ...]:
    units = _teaching_units(teaching.combined_text)
    dual_height_units = tuple(
        unit
        for unit in units
        if "模块05" in unit
        and _has_dual_height_contract(
            unit,
            rendered_height_literal=teaching.height_literal,
        )
    )
    forbidden_units = tuple(
        unit
        for unit in units
        if "模块05" in unit
        and _has_size_info_prohibition_contract(unit)
    )

    missing: list[str] = []
    if not dual_height_units:
        missing.extend(
            (
                "【尺寸标注信息】必须包含已确认高度字面",
                "【尺寸标注图规则】必须包含已确认高度字面",
            )
        )
    if not forbidden_units:
        missing.append("【尺寸标注信息】必须写明六个未确认参数禁止词")
    return tuple(missing)


def _assert_dimension_annotation_teaching_gate(
    root: Path,
) -> tuple[_CompiledDetailTeaching, ...]:
    teachings = tuple(
        _compile_detail_teaching(root, category_key)
        for category_key in _installed_category_keys(root)
    )
    violations: list[str] = []
    for teaching in teachings:
        violations.extend(
            f"{teaching.category_key}: {contract}"
            for contract in _missing_contracts(teaching)
        )
    if violations:
        raise AssertionError(
            "CAT-06 dimension-annotation teaching violation(s):\n"
            + "\n".join(violations)
        )
    return teachings


def _detail_source_paths(root: Path, category_key: str) -> tuple[Path, Path]:
    recipe_path = root / "categories" / category_key / "recipe.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    files = recipe.get("files")
    if not isinstance(files, dict):
        raise AssertionError(f"CAT-06 malformed recipe file map: {category_key}")
    prompt_path = recipe_path.parent / str(files["detail_prompt"])
    runtime_path = recipe_path.parent / str(files["detail_runtime"])
    return prompt_path, runtime_path


def _remove_dual_height_teaching(root: Path, category_key: str) -> tuple[int, int]:
    prompt_path, runtime_path = _detail_source_paths(root, category_key)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    updated_prompt, prompt_count = _DUAL_HEIGHT_TEMPLATE.subn("", prompt_text)
    prompt_path.write_text(updated_prompt, encoding="utf-8")

    package = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_count = 0
    for item in package["slices"]:
        updated_text, count = _DUAL_HEIGHT_TEMPLATE.subn("", item["text"])
        item["text"] = updated_text
        runtime_count += count
    runtime_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return prompt_count, runtime_count


class Cat06DimensionAnnotationTeachingGateTest(unittest.TestCase):
    def test_every_installed_category_teaches_the_complete_module05_contract(
        self,
    ) -> None:
        teachings = _assert_dimension_annotation_teaching_gate(ROOT)

        self.assertEqual(
            _installed_category_keys(ROOT),
            tuple(teaching.category_key for teaching in teachings),
        )
        for teaching in teachings:
            with self.subTest(category=teaching.category_key):
                self.assertFalse(_missing_contracts(teaching))

    def test_dual_height_contract_cannot_be_assembled_from_unrelated_claims(
        self,
    ) -> None:
        teaching = _CompiledDetailTeaching(
            category_key="反例品类",
            height_literal="高度约 8 厘米",
            rendered_prompt=(
                "模块05中，【尺寸标注信息】与【尺寸标注图规则】都必须包含来源说明。"
                "【尺寸标注信息】另处只说高度约 8 厘米，并明确禁止容量、宽度、"
                "直径、重量、材质。"
            ),
            runtime_text="",
        )

        missing = _missing_contracts(teaching)

        self.assertIn("【尺寸标注信息】必须包含已确认高度字面", missing)
        self.assertIn("【尺寸标注图规则】必须包含已确认高度字面", missing)
        self.assertNotIn("【尺寸标注信息】必须写明六个未确认参数禁止词", missing)

    def test_prohibition_contract_cannot_be_assembled_from_allowed_terms(
        self,
    ) -> None:
        teaching = _CompiledDetailTeaching(
            category_key="反例品类",
            height_literal="高度约 8 厘米",
            rendered_prompt=(
                "模块05中，【尺寸标注信息】与【尺寸标注图规则】都必须包含"
                "“高度约 8 厘米”。模块05的【尺寸标注信息】允许容量、宽度、"
                "直径、重量、材质。禁止删除字段。"
            ),
            runtime_text="",
        )

        missing = _missing_contracts(teaching)

        self.assertNotIn("【尺寸标注信息】必须包含已确认高度字面", missing)
        self.assertNotIn("【尺寸标注图规则】必须包含已确认高度字面", missing)
        self.assertIn("【尺寸标注信息】必须写明六个未确认参数禁止词", missing)

    def test_runtime_placeholder_can_supply_the_dual_height_contract(self) -> None:
        teaching = _CompiledDetailTeaching(
            category_key="运行切片反例品类",
            height_literal="高度约 8 厘米",
            rendered_prompt=(
                "模块05的【尺寸标注信息】必须明确禁止容量、宽度、直径、重量、材质。"
            ),
            runtime_text=(
                "模块05中，【尺寸标注信息】与【尺寸标注图规则】都必须包含"
                "“高度约 {height_cm} 厘米”。"
            ),
        )

        self.assertFalse(_missing_contracts(teaching))

    def test_removing_dual_height_teaching_from_one_category_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")

            target_key = ""
            removal_counts = (0, 0)
            for category_key in _installed_category_keys(temp_root):
                prompt_path, runtime_path = _detail_source_paths(
                    temp_root,
                    category_key,
                )
                prompt_count = len(
                    _DUAL_HEIGHT_TEMPLATE.findall(
                        prompt_path.read_text(encoding="utf-8")
                    )
                )
                runtime_count = len(
                    _DUAL_HEIGHT_TEMPLATE.findall(
                        runtime_path.read_text(encoding="utf-8")
                    )
                )
                if prompt_count and runtime_count:
                    target_key = category_key
                    removal_counts = _remove_dual_height_teaching(
                        temp_root,
                        category_key,
                    )
                    break

            self.assertTrue(target_key, "CAT-06 fault injection found no eligible category")
            self.assertGreater(removal_counts[0], 0)
            self.assertGreater(removal_counts[1], 0)
            with self.assertRaises(AssertionError) as caught:
                _assert_dimension_annotation_teaching_gate(temp_root)

            message = str(caught.exception)
            self.assertIn(target_key, message)
            self.assertIn("【尺寸标注信息】必须包含已确认高度字面", message)
            self.assertIn("【尺寸标注图规则】必须包含已确认高度字面", message)


if __name__ == "__main__":
    unittest.main()
