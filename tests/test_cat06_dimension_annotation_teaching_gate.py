from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


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
_OPTIONAL_DIMENSION_KEYS = ("length_cm", "width_cm")
_CLAUSE_BOUNDARY = re.compile(r"[。！？!?；;]")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")
_PROHIBITION_MARKER = re.compile(r"(?<!不)禁止")
_STANDALONE_HEADING = re.compile(r"^【[^】]+】$")
_FIELD_HEADINGS = ("【尺寸标注信息】", "【尺寸标注图规则】")
_FIELD_HEADING_PATTERN = re.compile("|".join(map(re.escape, _FIELD_HEADINGS)))
_MODULE05_REFERENCE_PATTERN = re.compile(
    r"不是产品尺寸标注图|非模块05|不是模块05|并非模块05|"
    r"不为模块05|不属于模块05|不属模块05|模块05(?:之外|以外)|模块05"
)
_TARGET_ACTION = (
    r"(?:写(?:明|出|入)?|填写|包含|采用|使用|保留|调用|选择|"
    r"固定为|设为)"
)
_POSITIVE_TARGET_GOVERNOR = re.compile(
    r"(?:必须|应当|一律|只能)[^。！？!?；;]{0,80}"
    + _TARGET_ACTION
    + r"[^。！？!?；;]{0,80}$"
)
_PLAIN_TARGET_ACTION = re.compile(
    r"(?:逐字\s*(?:写(?:明|出|入)?|保留)|固定\s*(?:写|为)|"
    r"写(?:明|出|入)?|填写|包含)"
    r"[^，,:：。！？!?；;]{0,32}$"
)
_DIRECT_TARGET_GOVERNOR = re.compile(
    r"(?:必须|应当|一律|只能)[^，,:：。！？!?；;]{0,16}$"
)
_NEGATIVE_TARGET_GOVERNOR = re.compile(
    r"(?:"
    r"(?:不得|禁止|严禁|不应|不能|不可|无需|不必|不要|避免|不再)"
    r"[^，,:：。！？!?；;]{0,24}"
    r"|(?:必须|应当|应|要|一律)?\s*"
    r"(?:取消|删除|删去|去掉|移除|停止|撤销|废止|替换)"
    r"(?:执行|使用|采用|写入|写|包含|保留|调用|选择|输出)?"
    r"[^，,:：。！？!?；;]{0,16}"
    r"|(?:必须|应当|一律)\s*不(?:再)?\s*"
    r"(?:写(?:明|出|入)?|填写|采用|使用|执行|包含|保留|调用|选择|输出)"
    r"[^，,:：。！？!?；;]{0,16}"
    r")\s*[“\"]?$"
)
_NEGATIVE_TARGET_CLASSIFICATION = re.compile(
    r"(?:错误(?:值|写法|示例)?|禁用(?:值|项|写法|示例)|"
    r"禁止(?:值|项|写法|示例)|"
    r"反例|示例|参考值|无效值|错误示例|错误要求|错误规则)"
    r"[^，,:：。！？!?；;]{0,12}$"
)
_NEGATIVE_TARGET_SUFFIX = re.compile(
    r"^[”\"']?[^。！？!?；;]{0,32}"
    r"(?:均|都|一律)?(?:不得|禁止|严禁|不应|不能|不可|无需|不必|不要|避免)"
    r"\s*(?:采用|使用|保留|写|调用|包含|选择|输出)"
)
_NEGATIVE_TARGET_CLASSIFICATION_SUFFIX = re.compile(
    r"^[”\"']?[^。！？!?；;]{0,32}(?:均|都)?(?:仅|只)?"
    r"(?:是|为|属于|作|作为|用作|当作|供)\s*"
    r"(?:错误(?:值|写法|示例)?|禁用(?:值|项|写法|示例)|"
    r"禁止(?:值|项|写法|示例)|"
    r"反例|示例|参考值|无效值|错误示例|错误要求|错误规则)"
)
_DUAL_HEIGHT_TEMPLATE = re.compile(
    r"【尺寸标注信息】与【尺寸标注图规则】(?:都|均)必须包含"
    r"[“\"]高度约\s*\{height_cm\}\s*厘米[”\"]"
)
_CUP_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE = (
    "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，必须在同栏明确区分"
    "已确认宽度与“禁止另行编造宽度”；如用户已确认长度，该长度同理必须逐字保留，"
    "不得被“未确认参数”禁止句削弱。"
)
_BOWL_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE = (
    "如用户已填写宽度，“宽度”禁止项不得删除该已填写宽度，必须在同栏明确区分"
    "已填写宽度与“禁止另行编造宽度”。"
)
_BOWL_UNCONDITIONAL_WIDTH_CLAUSE = (
    "这里的“宽度”禁止项不得删除用户已填写的宽，必须在同栏明确区分"
    "“已确认宽约 {width_cm} 厘米”与“禁止另行编造宽度”。"
)

_OUTPUT_RATIO_CONTRACT = "【输出画布比例】必须逐字写 3:4"
_DIMENSION_RATIO_HEIGHT_CONTRACT = "【尺寸比例锁定】必须包含已确认高度字面"
_DISABLED_HANDHELD_CONTRACT = "禁用手持必须逐字写本张图不启用手持场景"
_ENABLED_HANDHELD_CONTRACT = "启用手持必须写静态握持或动态拿起"
_STATIC_REFERENCE_CONTRACT = "静态握持参考图必须逐字写无，仅动态拿起场景可调用"
_DYNAMIC_REFERENCE_CONTRACT = "动态拿起未提供参考图必须逐字写未提供，不调用"
_NON_MODULE05_CONTRACT = "非模块05两栏必须逐字写非尺寸标注图"
_DUAL_HEIGHT_INFO_CONTRACT = "【尺寸标注信息】必须包含已确认高度字面"
_DUAL_HEIGHT_RULE_CONTRACT = "【尺寸标注图规则】必须包含已确认高度字面"
_PROHIBITION_CONTRACT = "【尺寸标注信息】必须写明六个未确认参数禁止词"
_OPTIONAL_DIMENSION_CONTRACT = "模块05禁止词与已确认可选尺寸必须共存消歧"
_RENDERED_DIMENSION_FACT_CONTRACT = "详情教学不得把未确认尺寸渲染为已确认事实"


@dataclass(frozen=True)
class _CompiledDetailTeaching:
    category_key: str
    height_literal: str
    rendered_prompt: str
    runtime_text: str
    optional_dimension_keys: tuple[str, ...] = ()
    confirmed_dimension_keys: tuple[str, ...] = ()
    combination_id: str = "all"


@dataclass(frozen=True)
class _ScopedTeachingUnit:
    text: str
    heading: str = ""
    module05_context: bool = False
    module05_inherited_context: bool = False
    size_info_context: bool = False


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


def _compile_detail_teaching(
    root: Path,
    category_key: str,
    *,
    dimension_values: Mapping[str, int] | None = None,
    combination_id: str = "all",
) -> _CompiledDetailTeaching:
    recipe = load_category_recipe(root, category_key)
    dimension_fields = recipe.form["dimensions"]["fields"]
    legal_dimensions = {
        str(field["key"]): _legal_dimension_value(field)
        for field in dimension_fields
    }
    dimensions = (
        legal_dimensions
        if dimension_values is None
        else {str(key): int(value) for key, value in dimension_values.items()}
    )
    required_dimensions = {
        str(key) for key in recipe.form["dimensions"]["required"]
    }
    optional_dimension_keys = tuple(
        key
        for key in _OPTIONAL_DIMENSION_KEYS
        if key in legal_dimensions and key not in required_dimensions
    )
    facts: dict[str, Any] = {
        "product_type": recipe.product_noun,
        **{key: dimensions.get(key) for key in legal_dimensions},
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
        optional_dimension_keys=optional_dimension_keys,
        confirmed_dimension_keys=tuple(
            key for key in legal_dimensions if key in dimensions
        ),
        combination_id=combination_id,
    )


def _dimension_combinations(
    root: Path,
    category_key: str,
) -> tuple[tuple[str, dict[str, int]], ...]:
    recipe = load_category_recipe(root, category_key)
    dimension_fields = recipe.form["dimensions"]["fields"]
    legal_dimensions = {
        str(field["key"]): _legal_dimension_value(field)
        for field in dimension_fields
    }
    required = {str(key) for key in recipe.form["dimensions"]["required"]}
    optional = tuple(key for key in legal_dimensions if key not in required)
    candidates: list[tuple[str, dict[str, int]]] = [
        ("all", dict(legal_dimensions)),
        (
            "required-only",
            {key: value for key, value in legal_dimensions.items() if key in required},
        ),
    ]
    candidates.extend(
        (
            f"missing-{missing_key}",
            {
                key: value
                for key, value in legal_dimensions.items()
                if key != missing_key
            },
        )
        for missing_key in optional
    )

    combinations: list[tuple[str, dict[str, int]]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for label, values in candidates:
        signature = tuple(sorted(values.items()))
        if signature in seen:
            continue
        seen.add(signature)
        combinations.append((label, values))
    return tuple(combinations)


def _teaching_units(text: str) -> tuple[str, ...]:
    units: list[str] = []
    for line in text.splitlines():
        for sentence in _SENTENCE_BOUNDARY.split(line):
            normalized = " ".join(sentence.split())
            if normalized:
                units.append(normalized)
    return tuple(units)


def _scoped_teaching_units(text: str) -> tuple[_ScopedTeachingUnit, ...]:
    units: list[_ScopedTeachingUnit] = []
    heading = ""
    for line in text.splitlines():
        module05_context = False
        for sentence in _SENTENCE_BOUNDARY.split(line):
            normalized = " ".join(sentence.split())
            if not normalized:
                continue
            inherited_module05_context = module05_context
            if _STANDALONE_HEADING.fullmatch(normalized):
                heading = normalized
            module05_polarity = _module05_reference_polarity(normalized)
            if module05_polarity is not None:
                module05_context = module05_polarity
            field_matches = tuple(_FIELD_HEADING_PATTERN.finditer(normalized))
            if field_matches:
                heading = field_matches[-1].group(0)
            units.append(
                _ScopedTeachingUnit(
                    text=normalized,
                    heading=heading,
                    module05_context=module05_context,
                    module05_inherited_context=inherited_module05_context,
                    size_info_context=heading == "【尺寸标注信息】",
                )
            )
    return tuple(units)


def _module05_reference_polarity(text: str) -> bool | None:
    references = tuple(_MODULE05_REFERENCE_PATTERN.finditer(text))
    if not references:
        return None
    return references[-1].group(0) == "模块05"


def _module05_context_at(scoped: _ScopedTeachingUnit, offset: int) -> bool:
    polarity = _module05_reference_polarity(scoped.text[: max(offset, 0)])
    if polarity is None:
        return scoped.module05_inherited_context
    return polarity


def _field_scoped_texts(
    scoped: _ScopedTeachingUnit,
    heading: str,
) -> tuple[str, ...]:
    matches = tuple(_FIELD_HEADING_PATTERN.finditer(scoped.text))
    if matches:
        segments: list[str] = []
        for index, match in enumerate(matches):
            if match.group(0) != heading:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else None
            segments.append(scoped.text[match.start() : end])
        return tuple(segments)
    if scoped.heading == heading and scoped.text != heading:
        return (scoped.text,)
    return ()


def _dual_field_shared_scope(text: str) -> str:
    forward = re.search(
        r"【尺寸标注信息】\s*(?:与|和|及|、)\s*【尺寸标注图规则】\s*"
        r"(?:两栏)?(?:都|均|分别|同时)\s*(?:必须|应当|一律|逐字)",
        text,
    )
    reverse = re.search(
        r"【尺寸标注图规则】\s*(?:与|和|及|、)\s*【尺寸标注信息】\s*"
        r"(?:两栏)?(?:都|均|分别|同时)\s*(?:必须|应当|一律|逐字)",
        text,
    )
    match = forward or reverse
    return text[match.start() :] if match is not None else ""


def _size_info_scoped_texts(
    scoped: _ScopedTeachingUnit,
) -> tuple[str, ...]:
    texts = list(_field_scoped_texts(scoped, "【尺寸标注信息】"))
    shared_scope = _dual_field_shared_scope(scoped.text)
    if shared_scope and shared_scope not in texts:
        texts.append(shared_scope)
    return tuple(texts)


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


def _has_size_info_prohibition_contract(
    unit: str,
    *,
    size_info_context: bool = False,
) -> bool:
    return bool(
        _size_info_prohibition_offsets(
            unit,
            size_info_context=size_info_context,
        )
    )


def _size_info_prohibition_offsets(
    unit: str,
    *,
    size_info_context: bool = False,
) -> tuple[int, ...]:
    size_info_index = unit.find("【尺寸标注信息】")
    if size_info_index < 0 and not size_info_context:
        return ()

    search_start = (
        size_info_index + len("【尺寸标注信息】")
        if size_info_index >= 0
        else 0
    )
    offsets: list[int] = []
    for match in _PROHIBITION_MARKER.finditer(unit, search_start):
        prefix = unit[search_start : match.start()]
        local_prefix = re.split(r"[，,:：]", prefix)[-1]
        if (
            not re.search(r"(?:必须|应当|一律)[^，,:：]{0,32}$", local_prefix)
            or re.search(
                r"(?:不再|取消|停止|撤销|不能|不可|不得|无需|不必|不要|避免)",
                local_prefix,
            )
        ):
            continue
        clause = _CLAUSE_BOUNDARY.split(unit[match.start() :], maxsplit=1)[0]
        if not all(term in clause for term in _UNCONFIRMED_PARAMETER_TERMS):
            continue
        first_term_index = min(
            clause.find(term) for term in _UNCONFIRMED_PARAMETER_TERMS
        )
        last_term_end = max(
            clause.find(term) + len(term) for term in _UNCONFIRMED_PARAMETER_TERMS
        )
        prohibited_action = clause[len(match.group(0)) : first_term_index]
        classification_suffix = clause[last_term_end:]
        if not any(
            action in prohibited_action
            for action in (
                "删除",
                "删去",
                "去掉",
                "遗漏",
                "省略",
                "移除",
                "改写",
            )
        ) and not _NEGATIVE_TARGET_CLASSIFICATION_SUFFIX.search(
            classification_suffix
        ):
            offsets.append(match.start())
    return tuple(offsets)


def _has_positive_target_instruction(unit: str, target: str) -> bool:
    return bool(_positive_target_instruction_offsets(unit, target))


def _positive_target_instruction_offsets(
    unit: str,
    target: str,
) -> tuple[int, ...]:
    offsets: list[int] = []
    for match in re.finditer(re.escape(target), unit):
        prefix = unit[: match.start()]
        local_prefix = re.split(r"[，,:：]", prefix)[-1]
        suffix = unit[match.end() :]
        if (
            _NEGATIVE_TARGET_GOVERNOR.search(local_prefix)
            or _NEGATIVE_TARGET_CLASSIFICATION.search(local_prefix)
            or _NEGATIVE_TARGET_SUFFIX.search(suffix)
            or _NEGATIVE_TARGET_CLASSIFICATION_SUFFIX.search(suffix)
        ):
            continue
        governed = (
            _POSITIVE_TARGET_GOVERNOR.search(prefix) is not None
            or _DIRECT_TARGET_GOVERNOR.search(local_prefix) is not None
        )
        plain_action = (
            _PLAIN_TARGET_ACTION.search(local_prefix) is not None
            and not any(
                marker in local_prefix
                for marker in ("可以写", "允许写", "可写", "可以包含", "允许包含")
            )
        )
        if governed or plain_action:
            offsets.append(match.start())
    return tuple(offsets)


def _has_output_ratio_contract(unit: str) -> bool:
    return (
        "输出画布比例" in unit
        and "3:4" in unit
        and _has_positive_target_instruction(unit, "3:4")
    )


def _has_dimension_ratio_height_contract(
    unit: str,
    *,
    rendered_height_literal: str,
) -> bool:
    height_fragment = rendered_height_literal.removeprefix("高度")
    match = re.search(_flexible_literal_pattern(height_fragment), unit)
    return (
        "尺寸比例锁定" in unit
        and match is not None
        and _has_positive_target_instruction(unit, match.group(0))
    )


def _has_disabled_handheld_contract(unit: str) -> bool:
    return (
        "本张图不启用手持场景" in unit
        and _has_positive_target_instruction(unit, "本张图不启用手持场景")
    )


def _has_enabled_handheld_contract(unit: str) -> bool:
    return (
        "启用" in unit
        and "静态握持" in unit
        and "动态拿起" in unit
        and _has_positive_target_instruction(unit, "静态握持")
        and _has_positive_target_instruction(unit, "动态拿起")
    )


def _has_static_reference_contract(unit: str) -> bool:
    return (
        "静态握持" in unit
        and "无，仅动态拿起场景可调用" in unit
        and _has_positive_target_instruction(unit, "无，仅动态拿起场景可调用")
    )


def _has_dynamic_reference_contract(unit: str) -> bool:
    return (
        "动态拿起" in unit
        and "参考图" in unit
        and "未提供，不调用" in unit
        and _has_positive_target_instruction(unit, "未提供，不调用")
    )


def _has_non_module05_both_fields_contract(
    scoped_units: tuple[_ScopedTeachingUnit, ...],
) -> bool:
    covered_fields: set[str] = set()
    for scoped in scoped_units:
        shared_scope = _dual_field_shared_scope(scoped.text)
        if shared_scope:
            shared_start = scoped.text.find(shared_scope)
            if any(
                _module05_reference_polarity(
                    scoped.text[: shared_start + target_offset]
                )
                is False
                for target_offset in _positive_target_instruction_offsets(
                    shared_scope,
                    "非尺寸标注图",
                )
            ):
                covered_fields.update(_FIELD_HEADINGS)

        for heading in _FIELD_HEADINGS:
            for field_text in _field_scoped_texts(scoped, heading):
                field_start = scoped.text.find(field_text)
                if any(
                    _module05_reference_polarity(
                        scoped.text[: field_start + target_offset]
                    )
                    is False
                    for target_offset in _positive_target_instruction_offsets(
                        field_text,
                        "非尺寸标注图",
                    )
                ):
                    covered_fields.add(heading)
    return covered_fields == set(_FIELD_HEADINGS)


def _has_conditional_dimension_prefix(unit: str, dimension: str) -> bool:
    return re.search(
        rf"(?:如|若|如果|当)用户已(?:确认|填写)[^，。！？!?；;]*{dimension}",
        unit,
    ) is not None


def _has_optional_dimension_disambiguation(
    units: tuple[str, ...],
    *,
    optional_dimension_keys: tuple[str, ...],
) -> bool:
    if "width_cm" in optional_dimension_keys:
        width_contract = any(
            "宽度" in unit
            and "同栏" in unit
            and "禁止另行编造宽度" in unit
            and any(marker in unit for marker in ("不得删除", "逐字保留"))
            and _has_conditional_dimension_prefix(unit, "宽度")
            and _has_positive_target_instruction(unit, "同栏明确区分")
            for unit in units
        )
        if not width_contract:
            return False
    if "length_cm" in optional_dimension_keys:
        length_contract = any(
            "长度" in unit
            and any(
                marker in unit
                for marker in ("未确认参数", "禁止另行编造长度")
            )
            and any(marker in unit for marker in ("不得删除", "逐字保留"))
            and _has_conditional_dimension_prefix(unit, "长度")
            and _has_positive_target_instruction(unit, "逐字保留")
            for unit in units
        )
        if not length_contract:
            return False
    return True


def _missing_dimension_is_stated_conditionally(unit: str, dimension_key: str) -> bool:
    dimension = "宽度" if dimension_key == "width_cm" else "长度"
    if _has_conditional_dimension_prefix(unit, dimension):
        return True
    short_dimension = "宽" if dimension_key == "width_cm" else "长"
    return re.search(
        rf"用户(?:另)?已(?:确认|填写)[^，。！？!?；;]*{short_dimension}[^，。！？!?；;]*时",
        unit,
    ) is not None


def _rendered_dimension_fact_problems(
    teaching: _CompiledDetailTeaching,
) -> tuple[str, ...]:
    prompt = teaching.rendered_prompt
    problems: list[str] = []
    if re.search(r"(?i)(?<![a-z0-9_])(?:none|null)(?![a-z0-9_])", prompt):
        problems.append("null-dimension-value")
    if re.search(r"(?:长|宽|高|长度|宽度|高度|口径)\s*约\s*厘米", prompt):
        problems.append("empty-dimension-value")
    if re.search(r"\{(?:length_cm|width_cm)\}", prompt):
        problems.append("unresolved-optional-dimension-placeholder")

    confirmed = set(teaching.confirmed_dimension_keys)
    units = _teaching_units(prompt)
    claim_patterns = {
        "length_cm": re.compile(
            r"(?:已确认|已填写)[^，。！？!?；;]{0,16}(?:长度|长约|长\s*\d)"
            r"|(?:长度|长约|长\s*\d)[^，。！？!?；;]{0,16}(?:已确认|已填写)"
        ),
        "width_cm": re.compile(
            r"(?:已确认|已填写)[^，。！？!?；;]{0,16}(?:宽度|宽约|宽\s*\d)"
            r"|(?:宽度|宽约|宽\s*\d)[^，。！？!?；;]{0,16}(?:已确认|已填写)"
        ),
    }
    for dimension_key in teaching.optional_dimension_keys:
        if dimension_key in confirmed:
            continue
        if any(
            claim_patterns[dimension_key].search(unit)
            and not _missing_dimension_is_stated_conditionally(unit, dimension_key)
            for unit in units
        ):
            problems.append(f"missing-{dimension_key}-stated-as-confirmed")
    return tuple(problems)


def _missing_contracts(teaching: _CompiledDetailTeaching) -> tuple[str, ...]:
    scoped_units = (
        _scoped_teaching_units(teaching.rendered_prompt)
        + _scoped_teaching_units(teaching.runtime_text)
    )
    units = tuple(scoped.text for scoped in scoped_units)
    dual_height_units = tuple(
        scoped.text
        for scoped in scoped_units
        if _has_dual_height_contract(
            scoped.text,
            rendered_height_literal=teaching.height_literal,
        )
        and _module05_context_at(scoped, scoped.text.find("高度约"))
    )
    forbidden_units = tuple(
        field_text
        for scoped in scoped_units
        for field_text in _size_info_scoped_texts(scoped)
        for prohibition_offset in _size_info_prohibition_offsets(
            field_text,
            size_info_context=True,
        )
        if _module05_context_at(
            scoped,
            scoped.text.find(field_text) + prohibition_offset,
        )
    )

    missing: list[str] = []
    if not any(_has_output_ratio_contract(unit) for unit in units):
        missing.append(_OUTPUT_RATIO_CONTRACT)
    if not any(
        _has_dimension_ratio_height_contract(
            unit,
            rendered_height_literal=teaching.height_literal,
        )
        for unit in units
    ):
        missing.append(_DIMENSION_RATIO_HEIGHT_CONTRACT)
    if not any(_has_disabled_handheld_contract(unit) for unit in units):
        missing.append(_DISABLED_HANDHELD_CONTRACT)
    if not any(_has_enabled_handheld_contract(unit) for unit in units):
        missing.append(_ENABLED_HANDHELD_CONTRACT)
    if not any(_has_static_reference_contract(unit) for unit in units):
        missing.append(_STATIC_REFERENCE_CONTRACT)
    if not any(_has_dynamic_reference_contract(unit) for unit in units):
        missing.append(_DYNAMIC_REFERENCE_CONTRACT)
    if not _has_non_module05_both_fields_contract(scoped_units):
        missing.append(_NON_MODULE05_CONTRACT)
    if not dual_height_units:
        missing.extend(
            (
                _DUAL_HEIGHT_INFO_CONTRACT,
                _DUAL_HEIGHT_RULE_CONTRACT,
            )
        )
    if not forbidden_units:
        missing.append(_PROHIBITION_CONTRACT)
    if teaching.optional_dimension_keys and not _has_optional_dimension_disambiguation(
        units,
        optional_dimension_keys=teaching.optional_dimension_keys,
    ):
        missing.append(_OPTIONAL_DIMENSION_CONTRACT)
    return tuple(missing)


def _compiled_dimension_combinations(
    root: Path,
    category_key: str,
) -> tuple[_CompiledDetailTeaching, ...]:
    return tuple(
        _compile_detail_teaching(
            root,
            category_key,
            dimension_values=values,
            combination_id=combination_id,
        )
        for combination_id, values in _dimension_combinations(root, category_key)
    )


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
        for rendered_teaching in _compiled_dimension_combinations(
            root,
            teaching.category_key,
        ):
            fact_problems = _rendered_dimension_fact_problems(rendered_teaching)
            if fact_problems:
                violations.append(
                    f"{teaching.category_key}: {_RENDERED_DIMENSION_FACT_CONTRACT} "
                    f"[{rendered_teaching.combination_id}]: "
                    + ", ".join(fact_problems)
                )
    if violations:
        raise AssertionError(
            "CAT-07 detail literal-contract teaching violation(s):\n"
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


def _remove_optional_dimension_disambiguation(
    root: Path,
    category_key: str,
) -> tuple[int, int]:
    prompt_path, runtime_path = _detail_source_paths(root, category_key)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_count = prompt_text.count(_CUP_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE)
    prompt_path.write_text(
        prompt_text.replace(_CUP_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE, ""),
        encoding="utf-8",
    )

    package = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_count = 0
    for item in package["slices"]:
        runtime_count += item["text"].count(
            _CUP_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE
        )
        item["text"] = item["text"].replace(
            _CUP_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE,
            "",
        )
    runtime_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return prompt_count, runtime_count


def _restore_bowl_unconditional_width_teaching(root: Path) -> tuple[int, int]:
    prompt_path, runtime_path = _detail_source_paths(root, "碗")
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_count = prompt_text.count(_BOWL_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE)
    prompt_path.write_text(
        prompt_text.replace(
            _BOWL_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE,
            _BOWL_UNCONDITIONAL_WIDTH_CLAUSE,
        ),
        encoding="utf-8",
    )

    package = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_count = 0
    for item in package["slices"]:
        runtime_count += item["text"].count(
            _BOWL_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE
        )
        item["text"] = item["text"].replace(
            _BOWL_OPTIONAL_DIMENSION_DISAMBIGUATION_CLAUSE,
            _BOWL_UNCONDITIONAL_WIDTH_CLAUSE,
        )
    runtime_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return prompt_count, runtime_count


def _complete_literal_contract_teaching(
    replacements: Mapping[int, str] | None = None,
) -> _CompiledDetailTeaching:
    clauses = {
        1: "【输出画布比例】必须逐字写 3:4。",
        2: "【尺寸比例锁定】必须包含约 8 厘米。",
        3: "禁用手持时必须逐字写“本张图不启用手持场景”。",
        4: "启用手持时必须写明静态握持或动态拿起。",
        5: "静态握持参考图必须逐字写“无，仅动态拿起场景可调用”。",
        6: "动态拿起参考图未提供时必须逐字写“未提供，不调用”。",
        7: (
            "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
            "都必须逐字写“非尺寸标注图”。"
        ),
        8: (
            "模块05中，【尺寸标注信息】与【尺寸标注图规则】"
            "都必须包含“高度约 8 厘米”。"
        ),
        9: (
            "模块05的【尺寸标注信息】必须明确禁止容量、宽度、直径、重量、材质。"
        ),
    }
    clauses.update(replacements or {})
    return _CompiledDetailTeaching(
        category_key="教学反例品类",
        height_literal="高度约 8 厘米",
        rendered_prompt="\n".join(clauses[number] for number in sorted(clauses)),
        runtime_text="",
    )


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
                "【尺寸标注信息】另处只说高度约 8 厘米，并且必须明确禁止容量、宽度、"
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
                "详情图【输出画布比例】必须固定写 3:4。\n"
                "所有【尺寸比例锁定】必须写约 8 厘米。\n"
                "禁用手持时逐字写“本张图不启用手持场景”。\n"
                "如果启用手持，必须写明静态握持或动态拿起。\n"
                "如果启用静态握持，逐字写“无，仅动态拿起场景可调用”。\n"
                "如果启用动态拿起且未提供参考图，逐字写“未提供，不调用”。\n"
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                "都逐字写“非尺寸标注图”。\n"
                "模块05的【尺寸标注信息】必须明确禁止容量、宽度、直径、重量、材质。"
            ),
            runtime_text=(
                "模块05中，【尺寸标注信息】与【尺寸标注图规则】都必须包含"
                "“高度约 {height_cm} 厘米”。"
            ),
        )

        self.assertFalse(_missing_contracts(teaching))

    def test_first_seven_contracts_reject_negative_writing_instructions(
        self,
    ) -> None:
        cases = (
            (1, _OUTPUT_RATIO_CONTRACT, "【输出画布比例】不得写 3:4。"),
            (2, _DIMENSION_RATIO_HEIGHT_CONTRACT, "【尺寸比例锁定】禁止写约 8 厘米。"),
            (
                3,
                _DISABLED_HANDHELD_CONTRACT,
                "禁用手持时不应写“本张图不启用手持场景”。",
            ),
            (
                4,
                _ENABLED_HANDHELD_CONTRACT,
                "启用手持时不得写静态握持或动态拿起。",
            ),
            (
                5,
                _STATIC_REFERENCE_CONTRACT,
                "静态握持参考图禁止写“无，仅动态拿起场景可调用”。",
            ),
            (
                6,
                _DYNAMIC_REFERENCE_CONTRACT,
                "动态拿起参考图不应写“未提供，不调用”。",
            ),
            (
                7,
                _NON_MODULE05_CONTRACT,
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                "都不得写“非尺寸标注图”。",
            ),
            (
                1,
                _OUTPUT_RATIO_CONTRACT,
                "【输出画布比例】必须写 1:1，严禁采用 3:4。",
            ),
            (
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                "【尺寸比例锁定】必须采用约 9 厘米，严禁采用约 8 厘米。",
            ),
            (
                3,
                _DISABLED_HANDHELD_CONTRACT,
                "禁用手持时必须删除“本张图不启用手持场景”。",
            ),
            (
                4,
                _ENABLED_HANDHELD_CONTRACT,
                "启用手持必须使用悬空展示，静态握持和动态拿起均不可采用。",
            ),
            (
                5,
                _STATIC_REFERENCE_CONTRACT,
                "静态握持参考图必须删除“无，仅动态拿起场景可调用”。",
            ),
            (
                6,
                _DYNAMIC_REFERENCE_CONTRACT,
                "动态拿起参考图必须删除“未提供，不调用”。",
            ),
        )
        for number, contract, negative_teaching in cases:
            with self.subTest(contract=number):
                missing = _missing_contracts(
                    _complete_literal_contract_teaching(
                        {number: negative_teaching}
                    )
                )
                self.assertIn(contract, missing)

    def test_first_seven_contracts_reject_every_cancelled_target_form(self) -> None:
        cases = (
            (
                1,
                _OUTPUT_RATIO_CONTRACT,
                {
                    "wrong-value": "【输出画布比例】必须写 1:1，3:4 是错误值。",
                    "cancel": "【输出画布比例】必须取消使用 3:4。",
                    "delete": "【输出画布比例】必须删除 3:4。",
                    "must-not": "【输出画布比例】必须不写 3:4。",
                },
            ),
            (
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                {
                    "wrong-value": (
                        "【尺寸比例锁定】必须包含约 9 厘米，约 8 厘米是禁用值。"
                    ),
                    "cancel": "【尺寸比例锁定】必须取消采用约 8 厘米。",
                    "delete": "【尺寸比例锁定】必须删除约 8 厘米。",
                    "must-not": "【尺寸比例锁定】必须不写约 8 厘米。",
                },
            ),
            (
                3,
                _DISABLED_HANDHELD_CONTRACT,
                {
                    "wrong-value": (
                        "禁用手持时必须写允许手持，本张图不启用手持场景是错误值。"
                    ),
                    "cancel": (
                        "禁用手持时必须取消执行本张图不启用手持场景。"
                    ),
                    "delete": "禁用手持时必须删除本张图不启用手持场景。",
                    "must-not": "禁用手持时必须不写本张图不启用手持场景。",
                },
            ),
            (
                4,
                _ENABLED_HANDHELD_CONTRACT,
                {
                    "wrong-value": (
                        "启用手持必须写悬空展示，静态握持与动态拿起都是禁用项。"
                    ),
                    "cancel": "启用手持必须取消使用静态握持或动态拿起。",
                    "delete": "启用手持必须删除静态握持与动态拿起。",
                    "must-not": "启用手持必须不写静态握持或动态拿起。",
                },
            ),
            (
                5,
                _STATIC_REFERENCE_CONTRACT,
                {
                    "wrong-value": (
                        "静态握持参考图必须写无，"
                        "无，仅动态拿起场景可调用是错误值。"
                    ),
                    "cancel": (
                        "静态握持参考图必须取消使用"
                        "无，仅动态拿起场景可调用。"
                    ),
                    "delete": (
                        "静态握持参考图必须删除无，仅动态拿起场景可调用。"
                    ),
                    "must-not": (
                        "静态握持参考图必须不写无，仅动态拿起场景可调用。"
                    ),
                },
            ),
            (
                6,
                _DYNAMIC_REFERENCE_CONTRACT,
                {
                    "wrong-value": (
                        "动态拿起参考图必须写无，未提供，不调用是错误值。"
                    ),
                    "cancel": "动态拿起参考图必须取消使用未提供，不调用。",
                    "delete": "动态拿起参考图必须删除未提供，不调用。",
                    "must-not": "动态拿起参考图必须不写未提供，不调用。",
                },
            ),
            (
                7,
                _NON_MODULE05_CONTRACT,
                {
                    "wrong-value": (
                        "非模块05两栏必须写任意值，非尺寸标注图是禁用值。"
                    ),
                    "cancel": (
                        "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                        "都必须取消使用非尺寸标注图。"
                    ),
                    "delete": (
                        "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                        "都必须删除非尺寸标注图。"
                    ),
                    "must-not": (
                        "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                        "都必须不写非尺寸标注图。"
                    ),
                },
            ),
        )
        for number, contract, attacks in cases:
            for attack, teaching_text in attacks.items():
                with self.subTest(contract=number, attack=attack):
                    missing = _missing_contracts(
                        _complete_literal_contract_teaching(
                            {number: teaching_text}
                        )
                    )
                    self.assertIn(contract, missing)

    def test_disabled_handheld_literal_internal_negation_remains_valid(self) -> None:
        teaching = _complete_literal_contract_teaching(
            {3: "禁用手持时必须逐字写“本张图不启用手持场景”。"}
        )

        self.assertNotIn(_DISABLED_HANDHELD_CONTRACT, _missing_contracts(teaching))

    def test_first_seven_contracts_reject_example_or_reference_targets(
        self,
    ) -> None:
        counterexamples = (
            (
                "ratio-example",
                1,
                _OUTPUT_RATIO_CONTRACT,
                "【输出画布比例】必须写 3:4，仅作示例。",
            ),
            (
                "ratio-reference",
                1,
                _OUTPUT_RATIO_CONTRACT,
                "【输出画布比例】必须写 3:4，该值是参考值。",
            ),
            (
                "height-example",
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                "【尺寸比例锁定】必须包含约 8 厘米，该值仅作示例。",
            ),
            (
                "height-reference",
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                "【尺寸比例锁定】必须包含约 8 厘米，该值是参考值。",
            ),
            (
                "disabled-example",
                3,
                _DISABLED_HANDHELD_CONTRACT,
                "禁用手持必须写本张图不启用手持场景，本句仅作示例。",
            ),
            (
                "enabled-reference",
                4,
                _ENABLED_HANDHELD_CONTRACT,
                "启用手持必须写静态握持或动态拿起，两项均是参考值。",
            ),
            (
                "static-error-rule",
                5,
                _STATIC_REFERENCE_CONTRACT,
                "静态握持参考图必须写无，仅动态拿起场景可调用，"
                "本句是错误规则。",
            ),
            (
                "dynamic-error-requirement",
                6,
                _DYNAMIC_REFERENCE_CONTRACT,
                "动态拿起参考图必须写未提供，不调用，本句是错误要求。",
            ),
            (
                "non-module-example",
                7,
                _NON_MODULE05_CONTRACT,
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                "都必须写非尺寸标注图，本句仅作反例。",
            ),
        )
        for label, number, contract, teaching_text in counterexamples:
            with self.subTest(case=label):
                teaching = _complete_literal_contract_teaching(
                    {number: teaching_text}
                )
                self.assertIn(contract, _missing_contracts(teaching))

        positive_lists = (
            (
                1,
                _OUTPUT_RATIO_CONTRACT,
                "【输出画布比例】必须固定写 3:4，图片宽度建议 1440px。",
            ),
            (
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                "【尺寸比例锁定】必须写长约 18 厘米、宽约 16 厘米、"
                "高度约 8 厘米。",
            ),
        )
        for number, contract, teaching_text in positive_lists:
            with self.subTest(valid_contract=number):
                teaching = _complete_literal_contract_teaching(
                    {number: teaching_text}
                )
                self.assertNotIn(contract, _missing_contracts(teaching))

    def test_contract_terms_cannot_be_assembled_across_sentences(self) -> None:
        cases = (
            (
                1,
                _OUTPUT_RATIO_CONTRACT,
                "【输出画布比例】必须逐字写固定值。另处示例为 3:4。",
            ),
            (
                2,
                _DIMENSION_RATIO_HEIGHT_CONTRACT,
                "【尺寸比例锁定】必须包含已确认高度。另处写约 8 厘米。",
            ),
            (
                3,
                _DISABLED_HANDHELD_CONTRACT,
                "禁用手持时必须逐字写声明。声明示例是“本张图不启用手持场景”。",
            ),
            (
                4,
                _ENABLED_HANDHELD_CONTRACT,
                "启用手持时必须写明子场景。可选词包括静态握持或动态拿起。",
            ),
            (
                5,
                _STATIC_REFERENCE_CONTRACT,
                "静态握持参考图必须逐字写固定句。"
                "固定句是“无，仅动态拿起场景可调用”。",
            ),
            (
                6,
                _DYNAMIC_REFERENCE_CONTRACT,
                "动态拿起参考图必须逐字写固定句。固定句是“未提供，不调用”。",
            ),
            (
                7,
                _NON_MODULE05_CONTRACT,
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】必须逐字写固定句。"
                "固定句是“非尺寸标注图”。",
            ),
            (
                9,
                _PROHIBITION_CONTRACT,
                "模块05的【尺寸标注信息】必须明确禁止容量、宽度。"
                "另处列出直径、重量、材质。",
            ),
        )
        for number, contract, split_teaching in cases:
            with self.subTest(contract=number):
                missing = _missing_contracts(
                    _complete_literal_contract_teaching(
                        {number: split_teaching}
                    )
                )
                self.assertIn(contract, missing)

    def test_prohibition_contract_rejects_prohibiting_field_deletion(self) -> None:
        counterexamples = (
            "模块05的【尺寸标注信息】必须明确禁止删除容量、宽度、直径、重量、材质。",
            "模块05的【尺寸标注信息】必须明确说明不再禁止容量、宽度、直径、重量、材质。",
            "模块05的【尺寸标注信息】必须取消禁止容量、宽度、直径、重量、材质。",
            "模块05的【尺寸标注信息】不能禁止容量、宽度、直径、重量、材质。",
            (
                "模块05的【尺寸标注信息】必须取消本栏继续禁止"
                "容量、宽度、直径、重量、材质。"
            ),
            (
                "模块05的【尺寸标注信息】必须明确禁止"
                "容量、宽度、直径、重量、材质，但这是错误要求。"
            ),
            (
                "模块05的【尺寸标注信息】必须明确禁止"
                "容量、宽度、直径、重量、材质，本条仅作反例。"
            ),
        )
        for counterexample in counterexamples:
            with self.subTest(teaching=counterexample):
                teaching = _complete_literal_contract_teaching(
                    {9: counterexample}
                )
                self.assertIn(_PROHIBITION_CONTRACT, _missing_contracts(teaching))

    def test_non_module05_contract_accepts_each_field_heading_context(self) -> None:
        teaching = _complete_literal_contract_teaching(
            {
                7: (
                    "【尺寸标注信息】\n"
                    "如果本张不是模块05，必须逐字写“非尺寸标注图”。\n"
                    "【尺寸标注图规则】\n"
                    "如果本张不是模块05，必须逐字写“非尺寸标注图”。"
                )
            }
        )

        self.assertNotIn(_NON_MODULE05_CONTRACT, _missing_contracts(teaching))

        one_field_only = _complete_literal_contract_teaching(
            {
                7: (
                    "非模块05的【尺寸标注信息】必须逐字写“非尺寸标注图”，"
                    "【尺寸标注图规则】可任意填写。"
                )
            }
        )
        self.assertIn(_NON_MODULE05_CONTRACT, _missing_contracts(one_field_only))

        switched_scope = _complete_literal_contract_teaching(
            {
                7: (
                    "非模块05的【尺寸标注信息】可任意，"
                    "【尺寸标注图规则】必须写非尺寸标注图，两栏分别处理。"
                )
            }
        )
        self.assertIn(_NON_MODULE05_CONTRACT, _missing_contracts(switched_scope))

        stitched_context = _complete_literal_contract_teaching(
            {
                7: (
                    "非模块05的其他字段可任意，模块05的"
                    "【尺寸标注信息】与【尺寸标注图规则】"
                    "都必须写非尺寸标注图。"
                )
            }
        )
        self.assertIn(
            _NON_MODULE05_CONTRACT,
            _missing_contracts(stitched_context),
        )

        marker_after_shared_target = _complete_literal_contract_teaching(
            {
                7: (
                    "【尺寸标注信息】与【尺寸标注图规则】"
                    "都必须写非尺寸标注图，非模块05另行说明。"
                )
            }
        )
        self.assertIn(
            _NON_MODULE05_CONTRACT,
            _missing_contracts(marker_after_shared_target),
        )

        marker_after_independent_targets = _complete_literal_contract_teaching(
            {
                7: (
                    "【尺寸标注信息】\n"
                    "必须写非尺寸标注图，非模块05另行说明。\n"
                    "【尺寸标注图规则】\n"
                    "必须写非尺寸标注图，非模块05另行说明。"
                )
            }
        )
        self.assertIn(
            _NON_MODULE05_CONTRACT,
            _missing_contracts(marker_after_independent_targets),
        )

    def test_module05_contracts_reject_explicit_non_module_context(self) -> None:
        cases = (
            (
                "dual-height",
                8,
                _DUAL_HEIGHT_INFO_CONTRACT,
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                "都必须包含“高度约 8 厘米”。",
            ),
            (
                "prohibition",
                9,
                _PROHIBITION_CONTRACT,
                "非模块05的【尺寸标注信息】必须明确禁止"
                "容量、宽度、直径、重量、材质。",
            ),
            (
                "positive-after-dual-height",
                8,
                _DUAL_HEIGHT_INFO_CONTRACT,
                "非模块05的【尺寸标注信息】与【尺寸标注图规则】"
                "都必须包含“高度约 8 厘米”，模块05另行说明。",
            ),
            (
                "positive-after-prohibition",
                9,
                _PROHIBITION_CONTRACT,
                "非模块05的【尺寸标注信息】必须明确禁止"
                "容量、宽度、直径、重量、材质，模块05另行说明。",
            ),
            (
                "nearest-non-module-dual-height",
                8,
                _DUAL_HEIGHT_INFO_CONTRACT,
                "模块05另行说明，非模块05的"
                "【尺寸标注信息】与【尺寸标注图规则】"
                "都必须包含“高度约 8 厘米”。",
            ),
            (
                "nearest-non-module-prohibition",
                9,
                _PROHIBITION_CONTRACT,
                "模块05另行说明，非模块05的【尺寸标注信息】"
                "必须明确禁止容量、宽度、直径、重量、材质。",
            ),
        )
        for label, number, contract, teaching_text in cases:
            with self.subTest(case=label, contract=number):
                teaching = _complete_literal_contract_teaching(
                    {number: teaching_text}
                )
                self.assertIn(contract, _missing_contracts(teaching))

    def test_prohibition_contract_cannot_be_supplied_by_rule_field(self) -> None:
        teaching = _complete_literal_contract_teaching(
            {
                9: (
                    "模块05的【尺寸标注信息】可任意，"
                    "【尺寸标注图规则】必须明确禁止"
                    "容量、宽度、直径、重量、材质。"
                )
            }
        )

        self.assertIn(_PROHIBITION_CONTRACT, _missing_contracts(teaching))

    def test_optional_dimension_lexicon_allowance_is_not_disambiguation(self) -> None:
        teaching = _CompiledDetailTeaching(
            category_key="可选尺寸反例品类",
            height_literal="高度约 8 厘米",
            rendered_prompt=(
                "模块05的【尺寸标注信息】与【尺寸标注图规则】都必须包含"
                "“高度约 8 厘米”，并明确禁止容量、宽度、直径、重量、材质。\n"
                "用户另已确认长约 18 厘米、宽约 16 厘米；"
                "模块05可同时标注这些已确认尺寸。"
            ),
            runtime_text="",
            optional_dimension_keys=("length_cm", "width_cm"),
        )

        self.assertIn(_OPTIONAL_DIMENSION_CONTRACT, _missing_contracts(teaching))

        reversed_teaching = _CompiledDetailTeaching(
            category_key="可选尺寸反向教学品类",
            height_literal="高度约 8 厘米",
            rendered_prompt=(
                "如用户已确认宽度，不得删除提示，但不得在同栏明确区分已确认宽度与"
                "“禁止另行编造宽度”；如用户已确认长度，该长度必须逐字保留，"
                "不得被“未确认参数”禁止句削弱。"
            ),
            runtime_text="",
            optional_dimension_keys=("length_cm", "width_cm"),
        )
        self.assertIn(
            _OPTIONAL_DIMENSION_CONTRACT,
            _missing_contracts(reversed_teaching),
        )

        cancelled_teachings = (
            _CompiledDetailTeaching(
                category_key="宽度消歧取消品类",
                height_literal="高度约 8 厘米",
                rendered_prompt=(
                    "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，"
                    "必须取消执行同栏明确区分已确认宽度与"
                    "“禁止另行编造宽度”。"
                ),
                runtime_text="",
                optional_dimension_keys=("width_cm",),
            ),
            _CompiledDetailTeaching(
                category_key="长度保留取消品类",
                height_literal="高度约 8 厘米",
                rendered_prompt=(
                    "如用户已确认长度，该长度必须取消使用逐字保留，"
                    "不得被“未确认参数”禁止句削弱。"
                ),
                runtime_text="",
                optional_dimension_keys=("length_cm",),
            ),
        )
        for cancelled in cancelled_teachings:
            with self.subTest(category=cancelled.category_key):
                self.assertIn(
                    _OPTIONAL_DIMENSION_CONTRACT,
                    _missing_contracts(cancelled),
                )

    def test_rendering_gate_rejects_every_invalid_optional_dimension_artifact(
        self,
    ) -> None:
        cases = (
            ("none", "已确认宽约 None 厘米", "null-dimension-value"),
            ("null", "已确认宽约 null 厘米", "null-dimension-value"),
            ("empty", "已确认宽约 厘米", "empty-dimension-value"),
            (
                "placeholder",
                "已确认宽约 {width_cm} 厘米",
                "unresolved-optional-dimension-placeholder",
            ),
            (
                "false-confirmed-fact",
                "用户已确认宽约 16 厘米",
                "missing-width_cm-stated-as-confirmed",
            ),
        )
        for label, rendered_prompt, expected_problem in cases:
            with self.subTest(case=label):
                teaching = _CompiledDetailTeaching(
                    category_key="可选尺寸渲染反例品类",
                    height_literal="高度约 8 厘米",
                    rendered_prompt=rendered_prompt,
                    runtime_text="",
                    optional_dimension_keys=("width_cm",),
                    confirmed_dimension_keys=("height_cm",),
                    combination_id="required-only",
                )
                self.assertIn(
                    expected_problem,
                    _rendered_dimension_fact_problems(teaching),
                )

    def test_every_legal_dimension_combination_renders_without_false_facts(
        self,
    ) -> None:
        expected_counts: dict[str, int] = {}
        actual_counts: dict[str, int] = {}
        for category_key in _installed_category_keys(ROOT):
            recipe = load_category_recipe(ROOT, category_key)
            dimension_keys = {
                str(field["key"])
                for field in recipe.form["dimensions"]["fields"]
            }
            required_keys = {
                str(key) for key in recipe.form["dimensions"]["required"]
            }
            optional_keys = dimension_keys - required_keys
            expected_signatures = {
                frozenset(dimension_keys),
                frozenset(required_keys),
                *(frozenset(dimension_keys - {key}) for key in optional_keys),
            }
            expected_counts[category_key] = len(expected_signatures)
            teachings = _compiled_dimension_combinations(ROOT, category_key)
            actual_counts[category_key] = len(teachings)
            for teaching in teachings:
                with self.subTest(
                    category=category_key,
                    combination=teaching.combination_id,
                ):
                    self.assertFalse(_rendered_dimension_fact_problems(teaching))

        self.assertEqual(expected_counts, actual_counts)

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

    def test_removing_optional_dimension_disambiguation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")

            self.assertIn("杯类", _installed_category_keys(temp_root))
            removal_counts = _remove_optional_dimension_disambiguation(
                temp_root,
                "杯类",
            )

            self.assertEqual((1, 1), removal_counts)
            with self.assertRaises(AssertionError) as caught:
                _assert_dimension_annotation_teaching_gate(temp_root)

            message = str(caught.exception)
            self.assertIn("杯类", message)
            self.assertIn(_OPTIONAL_DIMENSION_CONTRACT, message)

    def test_bowl_unconditional_width_placeholder_fails_rendering_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")

            self.assertEqual(
                (1, 1),
                _restore_bowl_unconditional_width_teaching(temp_root),
            )
            no_width = next(
                teaching
                for teaching in _compiled_dimension_combinations(temp_root, "碗")
                if "width_cm" not in teaching.confirmed_dimension_keys
            )
            self.assertIn(
                "null-dimension-value",
                _rendered_dimension_fact_problems(no_width),
            )

            with self.assertRaises(AssertionError) as caught:
                _assert_dimension_annotation_teaching_gate(temp_root)

            message = str(caught.exception)
            self.assertIn("碗", message)
            self.assertIn(_RENDERED_DIMENSION_FACT_CONTRACT, message)
            self.assertIn(f"[{no_width.combination_id}]", message)

    def test_required_only_bowl_rejects_false_width_through_real_compile(
        self,
    ) -> None:
        false_claim = "用户已确认宽约 16 厘米。"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")

            prompt_path, _ = _detail_source_paths(temp_root, "碗")
            prompt_path.write_text(
                prompt_path.read_text(encoding="utf-8") + f"\n{false_claim}\n",
                encoding="utf-8",
            )
            required_only = next(
                teaching
                for teaching in _compiled_dimension_combinations(temp_root, "碗")
                if teaching.combination_id == "required-only"
            )

            self.assertNotIn("width_cm", required_only.confirmed_dimension_keys)
            self.assertIn("width_cm", required_only.optional_dimension_keys)
            self.assertEqual(1, required_only.rendered_prompt.count(false_claim))
            self.assertIn(
                "missing-width_cm-stated-as-confirmed",
                _rendered_dimension_fact_problems(required_only),
            )

            with self.assertRaises(AssertionError) as caught:
                _assert_dimension_annotation_teaching_gate(temp_root)

            message = str(caught.exception)
            self.assertIn("碗", message)
            self.assertIn(_RENDERED_DIMENSION_FACT_CONTRACT, message)
            self.assertIn("[required-only]", message)
            self.assertIn("missing-width_cm-stated-as-confirmed", message)


if __name__ == "__main__":
    unittest.main()
