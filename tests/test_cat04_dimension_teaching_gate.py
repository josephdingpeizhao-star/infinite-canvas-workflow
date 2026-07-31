# CAT-04 不变式（顾问裁决）：本门禁只检查渲染后含“数字 + 厘米/cm”的已确认
# 尺寸教学句，并且只检查输出侧判据的“未确认参数”一族。材质与条件型教学句被
# 有意排除，因为 _reject_unsupported_claims 的设计对象是模型输出；把商品事实
# 启发式套到教学文本，会误拒“只有档案确认釉面时才呈现釉面”等合法条件规则。

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
    UserConfirmedRequirements,
    _reject_unsupported_claims,
    parse_user_confirmed_requirements,
)
from executor_contract import ExecutorExecutionError  # noqa: E402


_DIMENSION_VALUE = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*(?:厘米|cm)(?![A-Za-z])",
    flags=re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")
_PREFERRED_DIMENSIONS = {
    "length_cm": 18,
    "width_cm": 16,
    "height_cm": 8,
}
_EXPECTED_RATIO_BY_MODE = {
    "main": "1:1",
    "detail": "3:4",
    "final": "1:1",
}
_OLD_HEIGHT_LOCK = "约 {height_cm} 厘米的高度锁定"
_NEW_HEIGHT_LOCK = "高度约 {height_cm} 厘米的锁定"


@dataclass(frozen=True)
class _CategoryContext:
    key: str
    requirements: UserConfirmedRequirements
    confirmed_dimensions: dict[str, int]


@dataclass(frozen=True)
class _TeachingStatement:
    category_key: str
    source_id: str
    raw_template: str
    rendered_text: str


@dataclass(frozen=True)
class _GateScan:
    contexts: dict[str, _CategoryContext]
    statements: tuple[_TeachingStatement, ...]
    dimension_statements: tuple[_TeachingStatement, ...]
    corrected_height_locks: tuple[_TeachingStatement, ...]
    unexpected_parameter_violations: dict[str, str]


def _legal_dimension_value(field: dict[str, Any]) -> int:
    key = str(field["key"])
    preferred = _PREFERRED_DIMENSIONS[key]
    minimum = int(field["minimum"])
    maximum = int(field["maximum"])
    return min(max(preferred, minimum), maximum)


def _load_contexts(root: Path) -> dict[str, _CategoryContext]:
    contexts: dict[str, _CategoryContext] = {}
    for category_dir in sorted((root / "categories").iterdir()):
        if (
            not category_dir.is_dir()
            or category_dir.name.startswith("_")
            or not (category_dir / "recipe.json").is_file()
        ):
            continue

        recipe = load_category_recipe(root, category_dir.name)
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
                "category": category_dir.name,
                "user_confirmed_facts": facts,
            },
            root,
        )
        contexts[category_dir.name] = _CategoryContext(
            key=category_dir.name,
            requirements=requirements,
            confirmed_dimensions=dimensions,
        )
    if not contexts:
        raise AssertionError("CAT-04 did not discover any installed category")
    return contexts


def _split_sentences(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        sentence_number = 0
        for part in _SENTENCE_BOUNDARY.split(line):
            statement = part.strip()
            if not statement:
                continue
            sentence_number += 1
            result.append((line_number, sentence_number, statement))
    return result


def _render_statement(
    template: str,
    context: _CategoryContext,
    mode: str,
) -> str:
    values: dict[str, object] = {
        **context.confirmed_dimensions,
        "expected_ratio": _EXPECTED_RATIO_BY_MODE.get(mode, "1:1"),
    }
    rendered = template
    for key in ("length_cm", "width_cm", "height_cm", "expected_ratio"):
        rendered = rendered.replace(f"{{{key}}}", str(values[key]))
    return rendered


def _collect_statements(
    root: Path,
    contexts: dict[str, _CategoryContext],
) -> tuple[_TeachingStatement, ...]:
    statements: list[_TeachingStatement] = []
    for category_key, context in contexts.items():
        category_dir = root / "categories" / category_key
        for prompt_path in sorted((category_dir / "prompts").glob("*.md")):
            for line_number, sentence_number, template in _split_sentences(
                prompt_path.read_text(encoding="utf-8")
            ):
                statements.append(
                    _TeachingStatement(
                        category_key=category_key,
                        source_id=(
                            f"{category_key}:prompts/{prompt_path.name}:"
                            f"line:{line_number}:sentence:{sentence_number}"
                        ),
                        raw_template=template,
                        rendered_text=_render_statement(
                            template,
                            context,
                            prompt_path.stem,
                        ),
                    )
                )

        for runtime_path in sorted((category_dir / "runtime").glob("*.json")):
            package = json.loads(runtime_path.read_text(encoding="utf-8"))
            slices = package.get("slices")
            if not isinstance(slices, list):
                raise AssertionError(
                    f"CAT-04 runtime package has no slices: {runtime_path.name}"
                )
            for slice_item in slices:
                if not isinstance(slice_item, dict):
                    raise AssertionError(
                        f"CAT-04 malformed runtime slice in {runtime_path.name}"
                    )
                slice_id = slice_item.get("slice_id")
                text = slice_item.get("text")
                if not isinstance(slice_id, str) or not isinstance(text, str):
                    raise AssertionError(
                        f"CAT-04 malformed runtime slice in {runtime_path.name}"
                    )
                for line_number, sentence_number, template in _split_sentences(text):
                    statements.append(
                        _TeachingStatement(
                            category_key=category_key,
                            source_id=(
                                f"{category_key}:runtime/{runtime_path.name}:"
                                f"slice:{slice_id}:line:{line_number}:"
                                f"sentence:{sentence_number}"
                            ),
                            raw_template=template,
                            rendered_text=_render_statement(
                                template,
                                context,
                                runtime_path.stem,
                            ),
                        )
                    )
    return tuple(statements)


def _parameter_violation_message(
    statement: _TeachingStatement,
    context: _CategoryContext,
) -> str | None:
    try:
        _reject_unsupported_claims(
            {"prompts": [{"final_prompt": statement.rendered_text}]},
            context.requirements.height_cm,
            "CAT-04 尺寸教学句",
            product_type=context.requirements.product_type,
            lexicons=context.requirements.recipe.lexicons,
            confirmed_dimensions=context.confirmed_dimensions,
        )
    except ExecutorExecutionError as exc:
        message = str(exc)
        if "未确认参数" in message:
            return message
    return None


def _scan_dimension_teaching_gate(root: Path) -> _GateScan:
    contexts = _load_contexts(root)
    statements = _collect_statements(root, contexts)
    dimension_statements = tuple(
        statement
        for statement in statements
        if _DIMENSION_VALUE.search(statement.rendered_text)
    )
    corrected_height_locks = tuple(
        statement
        for statement in dimension_statements
        if _NEW_HEIGHT_LOCK in statement.raw_template
    )
    unexpected: dict[str, str] = {}
    for statement in dimension_statements:
        message = _parameter_violation_message(
            statement,
            contexts[statement.category_key],
        )
        if message is None:
            continue
        unexpected[statement.source_id] = message

    return _GateScan(
        contexts=contexts,
        statements=statements,
        dimension_statements=dimension_statements,
        corrected_height_locks=corrected_height_locks,
        unexpected_parameter_violations=unexpected,
    )


def _assert_dimension_teaching_gate(root: Path) -> _GateScan:
    scan = _scan_dimension_teaching_gate(root)
    if scan.unexpected_parameter_violations:
        details = "\n".join(
            f"{source_id}: {message}"
            for source_id, message in sorted(
                scan.unexpected_parameter_violations.items()
            )
        )
        raise AssertionError(
            "unexpected CAT-04 dimension-teaching violation(s):\n" + details
        )

    if len(scan.corrected_height_locks) != 4:
        raise AssertionError(
            "CAT-04 expected exactly four corrected height-lock statements, "
            f"found {len(scan.corrected_height_locks)}"
        )
    if any(_OLD_HEIGHT_LOCK in item.raw_template for item in scan.statements):
        raise AssertionError("CAT-04 old self-invalidating height-lock phrase remains")
    return scan


class Cat04DimensionTeachingGateTest(unittest.TestCase):
    def test_dimension_teaching_gate_has_zero_exemptions(self) -> None:
        scan = _assert_dimension_teaching_gate(ROOT)

        discovered = {
            path.name
            for path in (ROOT / "categories").iterdir()
            if path.is_dir()
            and not path.name.startswith("_")
            and (path / "recipe.json").is_file()
        }
        self.assertEqual(discovered, set(scan.contexts))
        self.assertFalse(scan.unexpected_parameter_violations)

    def test_material_condition_is_intentionally_outside_dimension_gate(self) -> None:
        scan = _scan_dimension_teaching_gate(ROOT)
        source_prefix = (
            "盘子:runtime/main.json:"
            "slice:plate-main-hf03-realism-runtime-summary-source:"
        )
        material_statement = next(
            statement
            for statement in scan.statements
            if statement.source_id.startswith(source_prefix)
            and "只有《产品身份档案》确认盘子为陶瓷或含釉面时" in statement.raw_template
        )

        self.assertIsNone(_DIMENSION_VALUE.search(material_statement.rendered_text))
        self.assertNotIn(material_statement, scan.dimension_statements)
        context = scan.contexts["盘子"]
        with self.assertRaisesRegex(ExecutorExecutionError, "未确认商品事实"):
            _reject_unsupported_claims(
                {"prompts": [{"final_prompt": material_statement.rendered_text}]},
                context.requirements.height_cm,
                "CAT-04 范围声明",
                product_type=context.requirements.product_type,
                lexicons=context.requirements.recipe.lexicons,
                confirmed_dimensions=context.confirmed_dimensions,
            )

    def test_old_height_lock_fault_injection_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")
            target = temp_root / "categories" / "盘子" / "prompts" / "final.md"
            original = target.read_text(encoding="utf-8")
            self.assertEqual(1, original.count(_NEW_HEIGHT_LOCK))
            target.write_text(
                original.replace(_NEW_HEIGHT_LOCK, _OLD_HEIGHT_LOCK, 1),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AssertionError,
                "unexpected CAT-04 dimension-teaching violation",
            ):
                _assert_dimension_teaching_gate(temp_root)


if __name__ == "__main__":
    unittest.main()
