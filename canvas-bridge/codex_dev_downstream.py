"""Validation and exclusive-write helpers for codex-dev downstream artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from category_recipes import (
    DEFAULT_CATEGORY_KEY,
    CategoryRecipe,
    CategoryRecipeError,
    load_category_recipe,
    load_manifest_category,
)
from executor_contract import ExecutorExecutionError


MAIN_REQUIRED_OVERRIDE_FIELDS = (
    "主图核心承诺",
    "绑定角度槽位",
    "角度适配原则",
    "产品角度依据",
    "产品颜色依据",
    "辅助参考图调用",
    "页面任务",
    "展示重点",
    "构图方式",
    "镜头距离",
    "产品位置",
    "产品占比",
    "尺寸比例锁定",
    "输出画布比例",
    "风格贴合锚点调用",
    "道具密度等级",
    "背景层次配置",
    "内容物状态",
    "道具生成",
    "手持交互声明",
    "动态手持样式参考图调用",
    "背景与光线",
    "文字信息",
)

DETAIL_REQUIRED_OVERRIDE_FIELDS = (
    "标准模块归属",
    "买家疑问",
    "信息来源与可用证据",
    "平台硬约束检查",
    "绑定角度槽位",
    "角度适配原则",
    "产品角度依据",
    "产品颜色依据",
    "辅助参考图调用",
    "页面任务",
    "展示重点",
    "镜头距离",
    "产品位置",
    "产品占比",
    "尺寸比例锁定",
    "输出画布比例",
    "尺寸标注信息",
    "尺寸标注图规则",
    "风格贴合锚点调用",
    "道具密度等级",
    "背景层次配置",
    "内容物状态",
    "构图方式",
    "文字信息",
    "中文营销文案",
    "文字渲染要求",
    "道具关系",
    "手持交互声明",
    "动态手持样式参考图调用",
    "背景与光线",
    "真实感要求",
    "风格防退化检查",
    "禁止事项",
)

_SEMANTIC_CONTEXT_POSITIVE = "positive_description"
_SEMANTIC_CONTEXT_NEGATIVE_LIST = "negative_list"
_SEMANTIC_CONTEXT_NON_SEMANTIC = "non_semantic"
_NON_SEMANTIC_OVERRIDE_FIELDS = frozenset(
    {"输出画布比例", "动态手持样式参考图调用"}
)
FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS = {
    "config_id": _SEMANTIC_CONTEXT_NON_SEMANTIC,
    "final_prompt": _SEMANTIC_CONTEXT_POSITIVE,
    "negative_prompt": _SEMANTIC_CONTEXT_NEGATIVE_LIST,
}
MAIN_VARIABLE_FIELD_SEMANTIC_CONTEXTS = {
    field: (
        _SEMANTIC_CONTEXT_NON_SEMANTIC
        if field in _NON_SEMANTIC_OVERRIDE_FIELDS
        else _SEMANTIC_CONTEXT_POSITIVE
    )
    for field in MAIN_REQUIRED_OVERRIDE_FIELDS
}
DETAIL_VARIABLE_FIELD_SEMANTIC_CONTEXTS = {
    field: (
        _SEMANTIC_CONTEXT_NEGATIVE_LIST
        if field == "禁止事项"
        else _SEMANTIC_CONTEXT_NON_SEMANTIC
        if field in _NON_SEMANTIC_OVERRIDE_FIELDS
        else _SEMANTIC_CONTEXT_POSITIVE
    )
    for field in DETAIL_REQUIRED_OVERRIDE_FIELDS
}
_CONTROL_VALUE_FIELDS = frozenset(
    {
        "artifact_type",
        "chunk_count",
        "chunk_index",
        "config_count",
        "output_type",
        "product_id",
        "resolved_variable_config_sha256",
        "用户要求主图手持数量",
        "用户要求详情图手持数量",
        "实际启用手持数量",
        "未启用手持数量",
        "启用手持配置",
        "是否完全满足用户数量",
    }
)
_SEMANTIC_FIELD_CONTEXTS = {
    **MAIN_VARIABLE_FIELD_SEMANTIC_CONTEXTS,
    **DETAIL_VARIABLE_FIELD_SEMANTIC_CONTEXTS,
    **FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS,
}

_VARIABLE_ALLOWED_TOP_LEVEL = {
    "common_constraints",
    "configs",
    "handheld_count_summary",
    "notes",
}
_VARIABLE_ALLOWED_CONFIG_FIELDS = {"config_id", "per_image_overrides", "notes"}
DETAIL_CHUNK_COUNT = 4
DETAIL_CONFIG_IDS_BY_CHUNK = tuple(
    tuple(f"detail_{index:02d}" for index in range(start, start + 2))
    for start in range(1, 9, 2)
)
_FORBIDDEN_DOWNSTREAM_KEYS = {
    "product_id",
    "artifact_type",
    "config_count",
    "upstream_artifacts",
    "output_type",
    "resolved_variable_config_sha256",
    "final_prompt",
    "final_prompts",
    "images",
    "image_outputs",
    "qc_results",
    "qc_reports",
    "set_layouts",
    "set_product_identity",
    "set_angle_layout_inventory",
    "comfyui_jobs",
}


@dataclass(frozen=True)
class UserConfirmedRequirements:
    product_type: str
    height_cm: int
    handheld_main: int
    handheld_detail: int
    allow_clear_water: bool
    forbid_pouring_and_heating: bool
    missing_d_no_retake: bool
    length_cm: int | None = None
    width_cm: int | None = None
    category: str = DEFAULT_CATEGORY_KEY
    recipe: CategoryRecipe | None = field(default=None, compare=False, repr=False)


_USER_CONFIRMED_FACT_KEYS = (
    "product_type",
    "height_cm",
    "handheld_main",
    "handheld_detail",
    "allow_clear_water",
    "forbid_pouring_and_heating",
    "missing_d_no_retake",
)
_CATEGORY_USER_CONFIRMED_FACT_KEYS = (
    "product_type",
    "length_cm",
    "width_cm",
    "height_cm",
    "handheld_main",
    "handheld_detail",
    "allow_clear_water",
    "forbid_pouring_and_heating",
    "missing_d_no_retake",
)


class DetailChunkTransportCorruption(ExecutorExecutionError):
    """A detail chunk was damaged in transport and may be resent in full."""


class DetailChunkEnvelopeCorrection(ExecutorExecutionError):
    """A safe detail chunk has the right identity but needs one envelope correction."""

    def __init__(self, business_fingerprint: str):
        super().__init__("detail chunk envelope needs correction")
        self.business_fingerprint = business_fingerprint


def _required_match(notes: str, pattern: str) -> str:
    match = re.search(pattern, notes)
    if not match:
        raise ValueError("missing requirement")
    return match.group(1)


def _yes_value(notes: str, label: str) -> bool:
    raw = _required_match(notes, rf"{re.escape(label)}\s*:\s*([^|]+)").strip()
    if raw not in {"是", "否"}:
        raise ValueError("invalid boolean requirement")
    return raw == "是"


def _validated_user_requirements(
    requirements: UserConfirmedRequirements,
    *,
    explicit_category: bool,
) -> UserConfirmedRequirements:
    recipe = requirements.recipe
    if recipe is None:
        raise ValueError("missing category recipe")
    if not requirements.product_type:
        raise ValueError("invalid requirement")
    if explicit_category and requirements.product_type != recipe.product_noun:
        raise ValueError("category product noun mismatch")
    field_metadata = {
        item["key"]: item for item in recipe.form["dimensions"]["fields"]
    }
    dimensions = {
        "length_cm": requirements.length_cm,
        "width_cm": requirements.width_cm,
        "height_cm": requirements.height_cm,
    }
    for key, value in dimensions.items():
        if value is None:
            if key in recipe.form["dimensions"]["required"]:
                raise ValueError("missing required dimension")
            continue
        metadata = field_metadata[key]
        if (
            type(value) is not int
            or value < metadata["minimum"]
            or value > metadata["maximum"]
        ):
            raise ValueError("invalid dimension")
    for mode, value in (
        ("main", requirements.handheld_main),
        ("detail", requirements.handheld_detail),
    ):
        metadata = recipe.form["handheld"][mode]
        if (
            type(value) is not int
            or value < metadata["minimum"]
            or value > metadata["maximum"]
        ):
            raise ValueError("invalid handheld count")
    return requirements


def _parse_structured_user_requirements(
    raw: Any,
    recipe: CategoryRecipe,
    *,
    explicit_category: bool,
) -> UserConfirmedRequirements:
    expected_keys = (
        _CATEGORY_USER_CONFIRMED_FACT_KEYS
        if explicit_category
        else _USER_CONFIRMED_FACT_KEYS
    )
    if not isinstance(raw, Mapping) or set(raw) != set(expected_keys):
        raise ValueError("invalid structured requirements")
    product_type = raw["product_type"]
    height_cm = raw["height_cm"]
    length_cm = raw.get("length_cm")
    width_cm = raw.get("width_cm")
    handheld_main = raw["handheld_main"]
    handheld_detail = raw["handheld_detail"]
    boolean_values = (
        raw["allow_clear_water"],
        raw["forbid_pouring_and_heating"],
        raw["missing_d_no_retake"],
    )
    if (
        not isinstance(product_type, str)
        or type(height_cm) is not int
        or (length_cm is not None and type(length_cm) is not int)
        or (width_cm is not None and type(width_cm) is not int)
        or type(handheld_main) is not int
        or type(handheld_detail) is not int
        or any(type(value) is not bool for value in boolean_values)
    ):
        raise ValueError("invalid structured requirement types")
    return _validated_user_requirements(
        UserConfirmedRequirements(
            product_type=product_type.strip(),
            height_cm=height_cm,
            handheld_main=handheld_main,
            handheld_detail=handheld_detail,
            allow_clear_water=boolean_values[0],
            forbid_pouring_and_heating=boolean_values[1],
            missing_d_no_retake=boolean_values[2],
            length_cm=length_cm,
            width_cm=width_cm,
            category=recipe.key,
            recipe=recipe,
        ),
        explicit_category=explicit_category,
    )


def parse_user_confirmed_requirements(
    manifest: Mapping[str, Any],
    repository_root: Path | None = None,
) -> UserConfirmedRequirements:
    """Read structured user facts, with notes retained only for legacy manifests."""

    try:
        root = repository_root or Path(__file__).resolve().parent.parent
        recipe = load_manifest_category(root, manifest)
        explicit_category = "category" in manifest
        if "user_confirmed_facts" in manifest:
            return _parse_structured_user_requirements(
                manifest["user_confirmed_facts"],
                recipe,
                explicit_category=explicit_category,
            )
        notes = str(manifest.get("notes") or "")
        return _validated_user_requirements(
            UserConfirmedRequirements(
                product_type=_required_match(notes, r"用户确认产品类型\s*:\s*([^|]+)").strip(),
                height_cm=int(_required_match(notes, r"用户确认高度厘米\s*:\s*(\d+)")),
                handheld_main=int(_required_match(notes, r"主图手持数量\s*:\s*(\d+)")),
                handheld_detail=int(_required_match(notes, r"详情图手持数量\s*:\s*(\d+)")),
                allow_clear_water=_yes_value(notes, "允许清水场景"),
                forbid_pouring_and_heating=_yes_value(notes, "禁止倾倒与加热"),
                missing_d_no_retake=_yes_value(notes, "D槽位不补拍"),
                category=recipe.key,
                recipe=recipe,
            ),
            explicit_category=False,
        )
    except (CategoryRecipeError, KeyError, TypeError, ValueError):
        raise ExecutorExecutionError("codex-dev 缺少有效的用户确认商品信息") from None


def _requirements_recipe(requirements: UserConfirmedRequirements) -> CategoryRecipe:
    if requirements.recipe is not None:
        return requirements.recipe
    try:
        return load_category_recipe(
            Path(__file__).resolve().parent.parent,
            requirements.category,
        )
    except CategoryRecipeError:
        raise ExecutorExecutionError("codex-dev 缺少有效的产品品类配方") from None


def _confirmed_dimensions(
    requirements: UserConfirmedRequirements,
) -> dict[str, int]:
    return {
        key: value
        for key, value in (
            ("length_cm", requirements.length_cm),
            ("width_cm", requirements.width_cm),
            ("height_cm", requirements.height_cm),
        )
        if value is not None
    }


def _artifact_entry(manifest: Mapping[str, Any], key: str) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ExecutorExecutionError(f"codex-dev 无法读取 {key} 产物位置")
    value = artifacts.get(key)
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
        if len(values) != 1:
            raise ExecutorExecutionError(f"codex-dev 无法读取 {key} 产物位置")
        return values[0]
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ExecutorExecutionError(f"codex-dev 无法读取 {key} 产物位置")


def artifact_file_under_root(
    manifest: Mapping[str, Any],
    artifact_key: str,
    filename: str,
) -> Path:
    """Resolve a declared artifact file and confine it to artifacts_root."""

    workspace = manifest.get("workspace")
    root_value = workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
    if not root_value:
        raise ExecutorExecutionError("codex-dev 无法验证 manifest.workspace.artifacts_root")
    try:
        artifacts_root = Path(str(root_value)).resolve()
        declared = Path(_artifact_entry(manifest, artifact_key))
        candidate = declared if declared.name == filename else declared / filename
        resolved = candidate.resolve()
        if not resolved.is_relative_to(artifacts_root):
            raise ExecutorExecutionError(
                f"{artifact_key} 输出位置不在 manifest.workspace.artifacts_root 内"
            )
        return resolved
    except ExecutorExecutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ExecutorExecutionError(f"codex-dev 无法验证 {artifact_key} 输出位置") from None


def load_typed_artifact(
    manifest: Mapping[str, Any],
    artifact_key: str,
    filename: str,
    expected_type: str,
    label: str,
) -> tuple[dict[str, Any], Path]:
    """Load one formal JSON artifact with type and product identity checks."""

    path = artifact_file_under_root(manifest, artifact_key, filename)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}") from None
    if not isinstance(value, dict) or value.get("artifact_type") != expected_type:
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}")
    product_id = str(manifest.get("product_id") or "")
    if not product_id or value.get("product_id") != product_id:
        raise ExecutorExecutionError(f"codex-dev 检测到{label}与当前商品不匹配")
    return value, path


def load_skill_runtime_package(
    repository_root: Path,
    skill_name: str,
    filename: str,
    label: str,
    category_recipe: CategoryRecipe | None = None,
) -> dict[str, Any]:
    """Load category-owned runtime rules; generic Skill text remains in .agents."""

    del filename
    try:
        recipe = category_recipe or load_manifest_category(repository_root, {})
        runtime_key = {
            "main-variable-config": "main_runtime",
            "detail-variable-config": "detail_runtime",
            "final-prompt-compiler": "final_runtime",
        }[skill_name]
        value = recipe.runtime_packages[runtime_key]
    except (CategoryRecipeError, KeyError):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}运行规则")
    return dict(value)


def qualified_angle_assets(angle_doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return qualified single-product A/B/C/D records keyed by source asset id."""

    missing = {
        str(slot).strip()
        for slot in angle_doc.get("missing_angle_slots", [])
        if isinstance(slot, str)
    }
    records = angle_doc.get("angle_slots")
    if not isinstance(records, list):
        raise ExecutorExecutionError("codex-dev 无法读取有效的角度槽位入库表")
    qualified: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise ExecutorExecutionError("codex-dev 无法读取有效的角度槽位入库表")
        asset_id = str(raw.get("source_asset_id") or "").strip()
        slot = str(raw.get("angle_slot") or "").strip()
        admission = str(raw.get("admission_result") or "").strip()
        if slot not in {"A", "B", "C", "D"} or admission == "不适合入库，需重拍":
            continue
        if slot in missing:
            continue
        if not asset_id or asset_id in qualified:
            raise ExecutorExecutionError("codex-dev 检测到角度槽位源图记录异常")
        qualified[asset_id] = dict(raw)
    if not qualified:
        raise ExecutorExecutionError("codex-dev 没有可用于后续配置的合格角度源图")
    return qualified


def _validate_missing_d_confirmation(
    angle_doc: Mapping[str, Any],
    requirements: UserConfirmedRequirements,
) -> None:
    missing = angle_doc.get("missing_angle_slots")
    if (
        not requirements.missing_d_no_retake
        and isinstance(missing, list)
        and any(isinstance(slot, str) and slot.strip() == "D" for slot in missing)
    ):
        raise ExecutorExecutionError("codex-dev 检测到 D 槽位缺失且尚未确认不补拍")


def stable_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_json_object(text: str, label: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}返回格式异常") from None
    if not isinstance(value, dict):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}返回格式异常")
    return value


def _walk_values(value: Any):
    yield value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_values(key)
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _walk_string_contexts(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            yield from _walk_string_contexts(item, (*path, key_text))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_string_contexts(item, (*path, str(index)))
    elif isinstance(value, str):
        yield path, value


def _semantic_context_for_path(path: tuple[str, ...]) -> str:
    if "handheld_count_summary" in path:
        return _SEMANTIC_CONTEXT_NON_SEMANTIC
    for part in reversed(path):
        if part in _SEMANTIC_FIELD_CONTEXTS:
            return _SEMANTIC_FIELD_CONTEXTS[part]
        if part in _CONTROL_VALUE_FIELDS:
            return _SEMANTIC_CONTEXT_NON_SEMANTIC
    return _SEMANTIC_CONTEXT_POSITIVE


_CLAUSE_SEPARATOR_PATTERN = r"[，,。；;\n]"
_NUMBER_PATTERN = r"\d+(?:\.\d+)?"
_LENGTH_UNIT_PATTERN = r"(?:毫米|mm|厘米|cm)"
_RANGE_CONNECTOR_PATTERN = r"(?:-|−|－|–|—|~|～|/|／|至|到)"
_EXISTING_FACT_PROTECTION_MARKERS = (
    "不得",
    "禁止",
    "不要",
    "不出现",
    "不生成",
    "避免",
    "未确认",
    "不确认",
    "不宣称",
    "不推断",
)
_DIMENSION_GROUP_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:"
    rf"{_NUMBER_PATTERN}\s*(?:{_LENGTH_UNIT_PATTERN})?\s*[×xX*]\s*"
    rf"{_NUMBER_PATTERN}\s*{_LENGTH_UNIT_PATTERN}"
    rf"|"
    rf"{_NUMBER_PATTERN}\s*{_LENGTH_UNIT_PATTERN}\s*[×xX*]\s*"
    rf"{_NUMBER_PATTERN}(?:\s*{_LENGTH_UNIT_PATTERN})?"
    rf")",
    flags=re.IGNORECASE,
)
_FACT_CLAUSE_SEPARATOR_PATTERN = re.compile(r"[。；;\n]+")
_SAFE_UNSUPPORTED_CLAIM_PATH_SEGMENTS = frozenset(
    {
        "chunk_index",
        "chunk_count",
        "common_constraints",
        "configs",
        "handheld_count_summary",
        "notes",
        "config_id",
        "per_image_overrides",
        "prompts",
        "final_prompt",
        "negative_prompt",
        "产品类型",
        "已确认高度",
        "事实边界",
        "动作边界",
        "页面链路",
        "尺寸比例",
        "用户要求主图手持数量",
        "用户要求详情图手持数量",
        "实际启用手持数量",
        "未启用手持数量",
        "启用手持配置",
        "是否完全满足用户数量",
        *MAIN_REQUIRED_OVERRIDE_FIELDS,
        *DETAIL_REQUIRED_OVERRIDE_FIELDS,
    }
)
_INDEXED_UNSUPPORTED_CLAIM_PATH_SEGMENTS = frozenset({"configs", "prompts"})


def _product_subject_terms(product_type: str | None) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", str(product_type or ""))
    if not normalized:
        return ()
    terms = {normalized}
    if len(normalized) > 1 and normalized.endswith("子"):
        terms.add(normalized[:-1])
    return tuple(sorted(terms, key=len, reverse=True))


def _is_product_directed_unsupported_fact(
    sentence: str,
    fact: re.Match[str],
    product_type: str | None,
    material_context_markers: Sequence[str],
) -> bool:
    """Require grammatical attachment to the product, not mere same-clause presence."""

    prefix = sentence[: fact.start()]
    suffix = sentence[fact.end() :]
    marker_pattern = "|".join(map(re.escape, material_context_markers))
    local_prefix_pattern = re.compile(
        rf"(?:{marker_pattern})"
        r"\s*(?:的\s*)?(?:主体\s*)?(?:材质\s*)?"
        r"(?:(?:为|是|不是|采用|使用|选用|由|具有|呈现?|[:：])\s*)?$"
    )
    local_suffix_pattern = re.compile(rf"\s*(?:的\s*)?(?:{marker_pattern})")
    if local_prefix_pattern.search(prefix):
        return True
    if local_suffix_pattern.match(suffix):
        return True

    subject_terms = _product_subject_terms(product_type)
    if any(
        re.search(
            rf"{re.escape(term)}\s*(?:的\s*)?(?:主体\s*)?(?:材质\s*)?"
            r"(?:(?:为|是|不是|采用|使用|选用|由|具有|呈现?|[:：])\s*)?$",
            prefix,
        )
        for term in subject_terms
    ):
        return True
    return any(
        re.match(
            rf"^[\u3400-\u9fffA-Za-z0-9_-]{{0,6}}{re.escape(term)}"
            r"(?=$|[\s，,。；;：:、/／（）()\[\]【】的为是])",
            suffix,
        )
        for term in subject_terms
    )


def _string_leaf_values(value: Any):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _string_leaf_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaf_values(item)
    elif isinstance(value, str) and value.strip():
        yield value.strip()


def style_master_material_reference_text(
    style_master: Mapping[str, Any],
    *,
    product_id: str,
) -> str:
    """Return validated formal style-master body text for downstream integrity."""

    if (
        not isinstance(style_master, Mapping)
        or style_master.get("artifact_type") != "style_master"
        or not isinstance(style_master.get("style_master"), Mapping)
    ):
        raise ExecutorExecutionError("codex-dev 无法读取有效的正式风格母版")
    if style_master.get("product_id") != product_id:
        raise ExecutorExecutionError("codex-dev 检测到正式风格母版与当前商品不匹配")
    text = "\n".join(_string_leaf_values(style_master["style_master"]))
    if not text:
        raise ExecutorExecutionError("codex-dev 无法读取有效的正式风格母版")
    return text


def _load_style_master_material_reference_text(path: Path, *, product_id: str) -> str:
    try:
        style_master = json.loads(path.read_text(encoding="utf-8"))
    except (AttributeError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ExecutorExecutionError("codex-dev 无法读取有效的正式风格母版") from None
    if not isinstance(style_master, Mapping):
        raise ExecutorExecutionError("codex-dev 无法读取有效的正式风格母版")
    return style_master_material_reference_text(style_master, product_id=product_id)


def _is_confirmed_dimension_measurement(
    text: str,
    path: tuple[str, ...],
    match: re.Match[str],
    number: float,
    confirmed_dimensions: Mapping[str, int],
    competing_dimension_terms: Sequence[str],
) -> bool:
    prefix = text[: match.start()]
    clause_prefix = re.split(_CLAUSE_SEPARATOR_PATTERN, prefix)[-1]
    clause_suffix = re.split(
        _CLAUSE_SEPARATOR_PATTERN,
        text[match.end() :],
        maxsplit=1,
    )[0]
    clause = f"{clause_prefix}{match.group(0)}{clause_suffix}"
    unit_extension = bool(
        re.match(
            r"\s*(?:[A-Za-z0-9_²³⁰¹⁴⁵⁶⁷⁸⁹^/／]|平方|立方)",
            text[match.end() :],
        )
    )
    negative_prefix = bool(re.search(r"(?:-|−|－|–|—)\s*$", clause_prefix))
    range_context = bool(
        re.search(
            rf"{_NUMBER_PATTERN}\s*{_RANGE_CONNECTOR_PATTERN}\s*(?:约\s*)?$",
            clause_prefix,
        )
        or re.match(
            rf"\s*{_RANGE_CONNECTOR_PATTERN}\s*(?:约\s*)?{_NUMBER_PATTERN}",
            clause_suffix,
        )
    )
    dimension_group = bool(_DIMENSION_GROUP_PATTERN.search(clause))
    if any((range_context, negative_prefix, unit_extension, dimension_group)):
        return False

    labels_by_key = {
        "length_cm": ("长度", "长"),
        "width_cm": ("宽度", "宽"),
        "height_cm": ("高度", "高"),
    }
    matching_keys = [
        key
        for key, value in confirmed_dimensions.items()
        if float(value) == number and key in labels_by_key
    ]
    for key in matching_keys:
        labels = "|".join(map(re.escape, labels_by_key[key]))
        label_before = re.search(
            rf"(?:{labels})\s*[：:]?\s*(?:为|是)?\s*"
            r"(?:约|大约|大概|近|about|approximately)?\s*$",
            clause_prefix,
            flags=re.IGNORECASE,
        )
        label_after = re.match(
            rf"\s*(?:{labels})(?=$|[\s的为是、:：()（）×xX*])",
            clause_suffix,
        )
        label_path = any(
            any(label in part for label in labels_by_key[key])
            for part in path
        )
        if label_before or label_after:
            return True
        semantic_context = (*path, clause)
        if label_path and not any(
            term not in labels_by_key[key]
            and term.casefold() in part.casefold()
            for part in semantic_context
            for term in competing_dimension_terms
        ):
            return True

    if "height_cm" not in matching_keys:
        return False
    semantic_context = (*path, clause)
    return not any(
        term.casefold() in part.casefold()
        for part in semantic_context
        for term in competing_dimension_terms
    )


def _format_unsupported_claims_error(
    label: str,
    violations: Sequence[tuple[str, tuple[str, ...]]],
) -> str:
    categories = list(dict.fromkeys(category for category, _path in violations))
    total = len(violations)
    prefix = f"codex-dev 收到的{label}包含{'、'.join(categories)}（{total} 处："
    suffix = "）"
    paths = ["/".join(path) if path else "$" for _category, path in violations]
    visible_paths: list[str] = []
    for index, path in enumerate(paths):
        omitted = total - index - 1
        candidate_parts = [*visible_paths, path]
        if omitted:
            candidate_parts.append(f"等 {omitted} 处")
        candidate = f"{prefix}{'；'.join(candidate_parts)}{suffix}"
        if len(candidate) > 200:
            break
        visible_paths.append(path)

    omitted = total - len(visible_paths)
    parts = [*visible_paths]
    if omitted:
        parts.append(f"等 {omitted} 处")
    message = f"{prefix}{'；'.join(parts)}{suffix}"
    if len(message) > 200:
        message = f"codex-dev 包含{'、'.join(categories)}（{total} 处：等 {total} 处）"
    return message


def _reject_unicode_damage_or_forbidden_keys(value: Mapping[str, Any], label: str) -> None:
    if any(isinstance(item, str) and "\ufffd" in item for item in _walk_values(value)):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}包含损坏字符")

    def inspect(mapping: Mapping[str, Any]) -> None:
        for key, item in mapping.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_DOWNSTREAM_KEYS or "套装" in normalized:
                raise ExecutorExecutionError(f"codex-dev 收到的{label}包含越界字段")
            if isinstance(item, Mapping):
                inspect(item)
            elif isinstance(item, list):
                for nested in item:
                    if isinstance(nested, Mapping):
                        inspect(nested)

    inspect(value)


def _reject_unsupported_claims(
    value: Mapping[str, Any],
    height_cm: int,
    label: str,
    *,
    product_type: str | None = None,
    lexicons: Mapping[str, Any] | None = None,
    confirmed_dimensions: Mapping[str, int] | None = None,
    style_master_text: str | None = None,
    defer_style_master_prop_materials: bool = False,
) -> None:
    if lexicons is None:
        try:
            lexicons = load_category_recipe(
                Path(__file__).resolve().parent.parent,
                DEFAULT_CATEGORY_KEY,
            ).lexicons
        except CategoryRecipeError:
            raise ExecutorExecutionError(
                f"codex-dev 无法读取有效的{label}品类词表"
            ) from None
    material_context_markers = lexicons.get("product_material_context_markers")
    unsupported_fact_terms = lexicons.get("unsupported_fact_terms")
    competing_dimension_terms = lexicons.get("competing_dimension_terms")
    if (
        not isinstance(material_context_markers, list)
        or not material_context_markers
        or not isinstance(unsupported_fact_terms, list)
        or not unsupported_fact_terms
        or not isinstance(competing_dimension_terms, list)
        or not competing_dimension_terms
    ):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}品类词表")
    fact_alternatives: list[str] = []
    safe_fact_alternatives: list[str] = []
    for term in unsupported_fact_terms:
        if term == "认证":
            fact_alternatives.append(r"通过.{0,8}认证")
            safe_fact_alternatives.append(r"通过[^，,。；;\n]{0,8}认证")
        else:
            escaped = re.escape(str(term))
            fact_alternatives.append(escaped)
            safe_fact_alternatives.append(escaped)
    suffix = r"(?:材质|质感|观感|工艺|属性|感)?"
    unsupported_fact_pattern = re.compile(
        rf"(?:{'|'.join(fact_alternatives)}){suffix}"
    )
    confirmed = dict(confirmed_dimensions or {"height_cm": height_cm})
    safe_negated_fact_token = (
        rf"(?:{'|'.join(safe_fact_alternatives)}){suffix}"
    )
    safe_negated_fact_target = (
        rf"{safe_negated_fact_token}"
        rf"(?:\s*(?:、|或|和|与|及|/|／)\s*{safe_negated_fact_token})*"
    )
    safe_negated_assignment_pattern = re.compile(
        r"不[ \t]*(?:把|将)[ \t]*"
        r"[^，,。；;\n]{1,64}?[ \t]*"
        r"(?:写死|固定|标注|设定|锁定|指定)[ \t]*"
        r"(?:为|成)[ \t]*"
        rf"(?P<safe_targets>{safe_negated_fact_target})[ \t]*"
        r"(?=$|[，,。；;\n])"
    )
    measurement_pattern = re.compile(
        r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?)\s*"
        r"(毫升|ml|升|l|毫米|mm|厘米|cm|克|g|千克|kg)",
        flags=re.IGNORECASE,
    )
    violations: list[tuple[str, tuple[str, ...]]] = []
    seen_violations: set[tuple[str, tuple[str, ...]]] = set()
    unknown_path_aliases: dict[tuple[str, ...], int] = {}

    def safe_path(path: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for index, part in enumerate(path):
            is_trusted_index = (
                part.isdigit()
                and index > 0
                and path[index - 1] in _INDEXED_UNSUPPORTED_CLAIM_PATH_SEGMENTS
            )
            if part in _SAFE_UNSUPPORTED_CLAIM_PATH_SEGMENTS or is_trusted_index:
                result.append(part)
                continue
            raw_prefix = path[: index + 1]
            alias = unknown_path_aliases.get(raw_prefix)
            if alias is None:
                alias = len(unknown_path_aliases) + 1
                unknown_path_aliases[raw_prefix] = alias
            result.append(f"未知字段{alias}")
        return tuple(result)

    def collect(category: str, path: tuple[str, ...]) -> None:
        violation = (category, safe_path(path))
        if violation not in seen_violations:
            seen_violations.add(violation)
            violations.append(violation)

    for path, item in _walk_string_contexts(value):
        if _semantic_context_for_path(path) != _SEMANTIC_CONTEXT_POSITIVE:
            continue
        if _DIMENSION_GROUP_PATTERN.search(item):
            collect("未确认参数", path)
        for match in measurement_pattern.finditer(item):
            number = float(match.group(1))
            unit = match.group(2).casefold()
            if (
                unit in {"厘米", "cm"}
                and _is_confirmed_dimension_measurement(
                    item,
                    path,
                    match,
                    number,
                    confirmed,
                    competing_dimension_terms,
                )
            ):
                continue
            collect("未确认参数", path)

        for sentence in _FACT_CLAUSE_SEPARATOR_PATTERN.split(item):
            if not sentence.strip():
                continue
            protected_spans = tuple(
                protected.span("safe_targets")
                for protected in safe_negated_assignment_pattern.finditer(sentence)
            )
            protected_by_existing_marker = any(
                marker in sentence for marker in _EXISTING_FACT_PROTECTION_MARKERS
            )
            for fact in unsupported_fact_pattern.finditer(sentence):
                if protected_by_existing_marker or any(
                    start <= fact.start() and fact.end() <= end
                    for start, end in protected_spans
                ):
                    continue
                if not _is_product_directed_unsupported_fact(
                    sentence,
                    fact,
                    product_type,
                    material_context_markers,
                ):
                    continue
                collect("未确认商品事实", path)

    if violations:
        raise ExecutorExecutionError(_format_unsupported_claims_error(label, violations))


_SCENE_NEGATION_MARKERS = (
    "禁止",
    "不得",
    "不要",
    "不可",
    "不允许",
    "不出现",
    "不生成",
    "避免",
    "未出现",
    "没有",
    "无清水",
)
_SCENE_ENUMERATION_NEGATION_PATTERN = re.compile(
    r"^\s*(?:不|无|未)[㐀-鿿]{1,2}[㐀-鿿]{2,12}"
    r"(?:(?:、|或|和|及|与)[㐀-鿿]{1,12})*"
    r"(?:、|或|和|及|与)\s*$"
)
_SCENE_ENUMERATION_SCOPE_BREAKERS = ("而", "但", "却", "仍", "再")
_SCENE_FIRST_ENUMERATION_NEGATION_HEAD_PATTERN = re.compile(
    r"^\s*(?:不|无|未)(?P<connecting_component>[㐀-鿿]{1,2})$"
)
_SCENE_FIRST_ENUMERATION_ADVERB_EXCLUSIONS = frozenset(
    ("慎", "小心", "停", "断", "住", "禁")
)
_SCENE_ENUMERATION_CONNECTORS = ("、", "或", "和", "及", "与")


def _term_is_first_negated_enumeration_item(
    clause: str,
    term_start: int,
    scanned_terms: Sequence[str],
) -> bool:
    head = _SCENE_FIRST_ENUMERATION_NEGATION_HEAD_PATTERN.fullmatch(
        clause[:term_start]
    )
    if not head:
        return False
    if (
        head.group("connecting_component")
        in _SCENE_FIRST_ENUMERATION_ADVERB_EXCLUSIONS
    ):
        return False
    if any(breaker in clause for breaker in _SCENE_ENUMERATION_SCOPE_BREAKERS):
        return False
    scanned_term = next(
        (
            term
            for term in scanned_terms
            if clause.startswith(term, term_start)
        ),
        None,
    )
    if scanned_term is None:
        return False
    return clause.startswith(
        _SCENE_ENUMERATION_CONNECTORS,
        term_start + len(scanned_term),
    )


def _term_has_scene_negation(
    clause: str,
    term_start: int,
    scanned_terms: Sequence[str],
) -> bool:
    prefix = clause[:term_start]
    existing_marker = any(marker in prefix for marker in _SCENE_NEGATION_MARKERS)
    existing_suffix = bool(
        re.search(r"(?:不|无|未)(?:会|再|进行|出现|使用|包含|装入|呈现)?\s*$", prefix)
    )
    negated_predicate = bool(
        re.search(r"(?:不|无|未)(?:再|予以)?(?:安排|计划|执行|展示|涉及|发生|允许)\s*$", prefix)
    )
    if existing_marker or existing_suffix or negated_predicate:
        return True
    if _term_is_first_negated_enumeration_item(clause, term_start, scanned_terms):
        return True
    enumerated_scope = _SCENE_ENUMERATION_NEGATION_PATTERN.fullmatch(prefix)
    return bool(
        enumerated_scope
        and not any(
            breaker in enumerated_scope.group(0)
            for breaker in _SCENE_ENUMERATION_SCOPE_BREAKERS
        )
    )


def _reject_scene_policy_violations(
    value: Mapping[str, Any],
    requirements: UserConfirmedRequirements,
    label: str,
) -> None:
    recipe = _requirements_recipe(requirements)
    content_terms = tuple(recipe.lexicons["scene_content_terms"])
    prohibited_action_terms = tuple(recipe.lexicons["prohibited_action_terms"])
    scanned_terms = (*content_terms, *prohibited_action_terms)
    exact_rule = _variable_scene_rule(requirements)
    for path, text in _walk_string_contexts(value):
        if _semantic_context_for_path(path) != _SEMANTIC_CONTEXT_POSITIVE:
            continue
        scene_text = text.replace(exact_rule, "").replace(requirements.product_type, "")
        if not requirements.allow_clear_water:
            for clause in re.split(r"[，,。；;\n]+", scene_text):
                content_positions = [
                    clause.find(term) for term in content_terms if term in clause
                ]
                if content_positions and not _term_has_scene_negation(
                    clause,
                    min(content_positions),
                    scanned_terms,
                ):
                    raise ExecutorExecutionError(
                        f"codex-dev 收到的{label}违反用户确认场景边界"
                    )
        if requirements.forbid_pouring_and_heating:
            for sentence in re.split(r"[。；;\n]+", scene_text):
                for clause in re.split(r"[，,]+", sentence):
                    term_positions = [
                        clause.find(term)
                        for term in prohibited_action_terms
                        if term in clause
                    ]
                    if not term_positions:
                        continue
                    if not _term_has_scene_negation(
                        clause,
                        min(term_positions),
                        scanned_terms,
                    ):
                        raise ExecutorExecutionError(
                            f"codex-dev 收到的{label}违反用户确认场景边界"
                        )


def _variable_scene_rule(requirements: UserConfirmedRequirements) -> str:
    scene_rules = _requirements_recipe(requirements).lexicons["scene_rules"]
    key = (
        ("water" if requirements.allow_clear_water else "no_water")
        + "_"
        + ("forbid_actions" if requirements.forbid_pouring_and_heating else "allow_actions")
    )
    return str(scene_rules[key])


def _final_forbidden_rule(requirements: UserConfirmedRequirements) -> str:
    prohibited = ["D", "被拒绝源图"]
    if not requirements.allow_clear_water:
        prohibited.append("清水场景")
    if requirements.forbid_pouring_and_heating:
        prohibited.extend(
            _requirements_recipe(requirements).lexicons["final_forbidden_action_terms"]
        )
    return (
        f"禁止 {'、'.join(prohibited)}，以及容量、其他尺寸、重量、具体材质、耐热、认证、"
        "品牌和型号等未确认事实。"
    )


def _load_skill_text(repository_root: Path, skill_name: str, label: str) -> str:
    try:
        text = (repository_root / ".agents" / "skills" / skill_name / "SKILL.md").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label} Skill") from None
    if not text.strip():
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label} Skill")
    return text


def _category_prompt_values(
    requirements: UserConfirmedRequirements,
    *,
    expected_ratio: str = "",
    handheld_count: int = 0,
) -> dict[str, object]:
    recipe = _requirements_recipe(requirements)
    material_examples = "".join(
        f"“{term}”" for term in recipe.lexicons["material_prompt_examples"]
    )
    product_material_term_rule = (
        f"所有字段中提及产品材质时，一律使用“材质”统称，不得写出{material_examples}"
        "等具体材质词；环境道具描述除外，但环境道具仍必须遵守正式风格母版与现有门禁。"
    )
    action_terms = "、".join(recipe.lexicons["prohibited_action_terms"])
    scene_safety_collective_rule = (
        f"表达内容物与动作安全边界时，不得逐词列举“{action_terms}”等禁止动作词；"
        "统一使用“不出现任何禁止的内容物或动作”这一统称表述，或原样复述本提示中的"
        "场景规则句，不得自行改写为禁止词清单。"
    )
    dimension_values = {
        "length_cm": requirements.length_cm,
        "width_cm": requirements.width_cm,
        "height_cm": requirements.height_cm,
    }
    required_dimensions = set(recipe.form["dimensions"]["required"])
    field_metadata = {
        item["key"]: item for item in recipe.form["dimensions"]["fields"]
    }
    optional_dimension_text = "、".join(
        f"{field_metadata[key]['label']}约 {value} 厘米"
        for key, value in dimension_values.items()
        if key not in required_dimensions and value is not None
    )
    optional_templates = recipe.lexicons["optional_dimension_prompts"]
    optional_prompts = {
        mode: (
            str(optional_templates[mode]).format(dimensions=optional_dimension_text)
            if optional_dimension_text
            else ""
        )
        for mode in ("main", "detail", "final")
    }
    return {
        "length_cm": requirements.length_cm,
        "width_cm": requirements.width_cm,
        "height_cm": requirements.height_cm,
        "expected_ratio": expected_ratio,
        "handheld_phrase": recipe.lexicons["handheld_phrase"],
        "handheld_count_text": (
            "恰好一项" if handheld_count == 1 else f"恰好 {handheld_count} 项"
        ),
        "product_material_term_rule": product_material_term_rule,
        "scene_safety_collective_rule": scene_safety_collective_rule,
        "scene_rule": _variable_scene_rule(requirements),
        "optional_dimensions_main": optional_prompts["main"],
        "optional_dimensions_detail": optional_prompts["detail"],
        "optional_dimensions_final": optional_prompts["final"],
    }


def build_variable_config_prompt(
    *,
    mode: str,
    product_id: str,
    repository_root: Path,
    identity: Mapping[str, Any],
    style_master: Mapping[str, Any],
    angle_inventory: Mapping[str, Any],
    requirements: UserConfirmedRequirements,
    main_variable_config: Mapping[str, Any] | None = None,
) -> str:
    """Build a self-contained downstream turn without attaching source images."""

    if mode not in {"main", "detail"}:
        raise ExecutorExecutionError("codex-dev 收到不支持的变量配置模式")
    skill_name = "main-variable-config" if mode == "main" else "detail-variable-config"
    label = "主图变量配置" if mode == "main" else "详情图变量配置"
    recipe = _requirements_recipe(requirements)
    skill_text = _load_skill_text(repository_root, skill_name, label)
    runtime = load_skill_runtime_package(
        repository_root,
        skill_name,
        f"runtime_rule_slices/{skill_name}.runtime_rule_slices.json",
        label,
        recipe,
    )
    _validate_missing_d_confirmation(angle_inventory, requirements)
    qualified = qualified_angle_assets(angle_inventory)
    allowed_angles = [
        qualified[key]
        for key in sorted(qualified)
        if str(qualified[key].get("angle_slot") or "").strip() in {"A", "B", "C"}
    ]
    facts = {
        "product_type": requirements.product_type,
        "height": f"约 {requirements.height_cm} 厘米",
        "handheld_main": requirements.handheld_main,
        "allow_clear_water": requirements.allow_clear_water,
        "forbid_pouring_and_heating": requirements.forbid_pouring_and_heating,
        "missing_d_no_retake": requirements.missing_d_no_retake,
        "unconfirmed": [
            "容量",
            "宽度",
            "直径",
            "重量",
            "具体材质",
            "耐热性能",
            "认证",
            "品牌",
            "型号",
        ],
    }
    if requirements.length_cm is not None:
        facts["length"] = f"约 {requirements.length_cm} 厘米"
    if requirements.width_cm is not None:
        facts["width"] = f"约 {requirements.width_cm} 厘米"
    prompt_values = _category_prompt_values(
        requirements,
        handheld_count=(
            requirements.handheld_main if mode == "main" else requirements.handheld_detail
        ),
    )
    if mode == "main":
        category_prompt = recipe.render_prompt(
            "main_prompt",
            **prompt_values,
        ).rstrip("\r\n")
        return f"""你正在为单品批次 {product_id} 生成主图变量配置，且只处理 main_vc。
这是结构化配置阶段，不生成图片、不生成最终提示词、不生成 ComfyUI 作业、不执行 QC，也不处理套装。
必须生成且只生成 main_01 至 main_06 六项，输出画布比例全部为 1:1，恰好 {requirements.handheld_main} 项启用手持。
每项只允许绑定下面列出的合格 A/B/C 源图中的一张，禁止 D、缺失槽位和所有被拒绝源图。
每项“绑定角度槽位”字段必须同时写出唯一合格源图编号，并原样包含“X 槽位”或“槽位 X”字样；X 必须是该源图实际对应的 A/B/C 槽位。
每项 per_image_overrides 必须恰好包含这些字段：{json.dumps(MAIN_REQUIRED_OVERRIDE_FIELDS, ensure_ascii=False)}
主图每项“辅助参考图调用”中的“对应产品”必须只原样填写本批 product_id：{product_id}；不得填写产品外观、材质、品类昵称或其他描述性名称。
{prompt_values["product_material_term_rule"]}
顶层只允许 common_constraints、configs、handheld_count_summary、notes；每项只允许 config_id、per_image_overrides、notes。
handheld_count_summary 使用业务字段：用户要求主图手持数量、实际启用手持数量、未启用手持数量、启用手持配置、是否完全满足用户数量。
动态手持样式参考图调用必须服从 canonical 值：不手持写“无”；静态握持写“无，仅动态拿起场景可调用”；动态拿起因未提供专用参考图写“未提供，不调用”。
{category_prompt}
只返回一个 JSON 对象，不要 Markdown 或额外说明。不要返回 product_id、artifact_type、config_count、upstream_artifacts、output_type、哈希、最终提示词、图片、QC 或套装字段。

【Skill 原文】
{skill_text}

【运行规则包】
{json.dumps(runtime, ensure_ascii=False, sort_keys=True)}

【用户确认事实】
{json.dumps(facts, ensure_ascii=False, sort_keys=True)}

【产品身份档案】
{json.dumps(identity, ensure_ascii=False, sort_keys=True)}

【风格母版】
{json.dumps(style_master, ensure_ascii=False, sort_keys=True)}

【仅允许绑定的角度记录】
{json.dumps(allowed_angles, ensure_ascii=False, sort_keys=True)}
"""
    if not isinstance(main_variable_config, Mapping):
        raise ExecutorExecutionError("codex-dev 缺少有效的正式主图变量配置")
    category_prompt = recipe.render_prompt(
        "detail_prompt",
        **prompt_values,
    ).rstrip("\r\n")
    return f"""你正在为单品批次 {product_id} 生成详情图变量配置，且只处理 detail_vc。
这是结构化配置阶段，不生成图片、不生成最终提示词、不生成 ComfyUI 作业、不执行 QC，也不处理套装。
必须生成且只生成 detail_01 至 detail_08 八项；标准模块归属依次且唯一为模块01至模块08；输出画布比例全部为 3:4；恰好 {requirements.handheld_detail} 项启用手持。
每项只允许绑定下面列出的合格 A/B/C 源图中的一张，禁止 D、缺失槽位和所有被拒绝源图。
每项“绑定角度槽位”字段必须同时写出唯一合格源图编号，并原样包含“X 槽位”或“槽位 X”字样；X 必须是该源图实际对应的 A/B/C 槽位。
每项 per_image_overrides 必须恰好包含这些字段：{json.dumps(DETAIL_REQUIRED_OVERRIDE_FIELDS, ensure_ascii=False)}
详情图每项“辅助参考图调用”中的“对应产品”必须只原样填写本批 product_id：{product_id}；不得填写产品外观、材质、品类昵称或其他描述性名称。
{prompt_values["product_material_term_rule"]}
顶层只允许 common_constraints、configs、handheld_count_summary、notes；每项只允许 config_id、per_image_overrides、notes。
handheld_count_summary 使用业务字段：用户要求详情图手持数量、实际启用手持数量、未启用手持数量、启用手持配置、是否完全满足用户数量。
动态手持样式参考图调用必须服从 canonical 值：不手持写“无”；静态握持写“无，仅动态拿起场景可调用”；动态拿起因未提供专用参考图写“未提供，不调用”。
{category_prompt}
模块01须承接正式主图配置中的已支持核心承诺，但不得复制或新增任何未确认说法。
只返回一个 JSON 对象，不要 Markdown 或额外说明。不要返回 product_id、artifact_type、config_count、upstream_artifacts、output_type、哈希、最终提示词、图片、QC 或套装字段。

【Skill 原文】
{skill_text}

【运行规则包】
{json.dumps(runtime, ensure_ascii=False, sort_keys=True)}

【用户确认事实】
{json.dumps(facts, ensure_ascii=False, sort_keys=True)}

【产品身份档案】
{json.dumps(identity, ensure_ascii=False, sort_keys=True)}

【风格母版】
{json.dumps(style_master, ensure_ascii=False, sort_keys=True)}

【正式主图变量配置】
{json.dumps(main_variable_config, ensure_ascii=False, sort_keys=True)}

【仅允许绑定的角度记录】
{json.dumps(allowed_angles, ensure_ascii=False, sort_keys=True)}
"""


def build_detail_variable_config_chunk_prompt(
    base_prompt: str,
    chunk_index: int,
    *,
    repair: bool = False,
    structure_correction: bool = False,
) -> str:
    """Request one bounded detail-config chunk from the same Codex thread."""

    if not 1 <= chunk_index <= DETAIL_CHUNK_COUNT:
        raise ExecutorExecutionError("codex-dev 收到无效的详情图变量配置分段编号")
    if repair and structure_correction:
        raise ExecutorExecutionError("codex-dev 收到冲突的详情图变量配置恢复请求")
    expected_ids = DETAIL_CONFIG_IDS_BY_CHUNK[chunk_index - 1]
    allowed_keys = ["chunk_index", "chunk_count", "configs"]
    if chunk_index == 1:
        allowed_keys.extend(("common_constraints", "notes"))
    if chunk_index == DETAIL_CHUNK_COUNT:
        allowed_keys.append("handheld_count_summary")

    if structure_correction:
        opening = (
            f"继续同一 detail_vc 任务。上一个第 {chunk_index}/{DETAIL_CHUNK_COUNT} 段未通过包装格式门禁；"
            f"请完整重发第 {chunk_index}/{DETAIL_CHUNK_COUNT} 段。不得引用、解释或局部修补上一段正文，"
            "不得改变配置内容、段号、配置编号或增加商品事实。"
        )
    elif repair:
        opening = (
            f"继续同一 detail_vc 任务。上一个第 {chunk_index}/{DETAIL_CHUNK_COUNT} 段未通过传输完整性门禁；"
            f"请完整重发第 {chunk_index}/{DETAIL_CHUNK_COUNT} 段。不得修补、引用或解释损坏正文。"
        )
    elif chunk_index == 1:
        opening = base_prompt + "\n\n"
    else:
        opening = "继续同一 detail_vc 任务。"

    return (
        opening
        + f"\n本轮只返回第 {chunk_index}/{DETAIL_CHUNK_COUNT} 段，且只包含配置 "
        + "、".join(expected_ids)
        + "。"
        + f"顶层键必须恰好为：{json.dumps(allowed_keys, ensure_ascii=False)}。"
        + f"chunk_index 必须为 {chunk_index}，chunk_count 必须为 {DETAIL_CHUNK_COUNT}。"
        + "configs 必须按上述顺序包含两项，每项只包含 config_id、per_image_overrides、notes。"
        + (
            "common_constraints 必须是非空 JSON 对象；notes 必须是 JSON 字符串，不能是对象或数组，"
            "也不能把 handheld_count_summary 放入 notes；两者只在本段返回。"
            if chunk_index == 1
            else ""
        )
        + (
            "handheld_count_summary 必须是 JSON 对象，只在本段返回，并汇总完整八项配置。"
            if chunk_index == DETAIL_CHUNK_COUNT
            else ""
        )
        + "只返回一个完整 JSON 对象，不要 Markdown、代码围栏或额外说明。"
    )


def _json_prefix_state(text: str) -> tuple[list[str], bool, bool, bool]:
    stack: list[str] = []
    in_string = False
    escaped = False
    root_started = False
    root_completed = False
    pairs = {"}": "{", "]": "["}
    for character in text:
        if root_completed:
            if not character.isspace():
                return stack, in_string, escaped, False
            continue
        if not root_started:
            if character.isspace():
                continue
            if character != "{":
                return stack, in_string, escaped, False
            root_started = True
            stack.append(character)
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or stack[-1] != pairs[character]:
                return stack, in_string, escaped, False
            stack.pop()
            root_completed = not stack
    return stack, in_string, escaped, root_started


def _is_probable_json_truncation(text: str, error: json.JSONDecodeError) -> bool:
    candidate = text.rstrip()
    if not candidate:
        return False
    stack, in_string, _dangling_escape, structurally_valid = _json_prefix_state(candidate)
    if not structurally_valid or (not stack and not in_string):
        return False
    if in_string:
        if error.msg.startswith("Unterminated string"):
            return True
        return (
            error.msg.startswith("Invalid \\uXXXX escape")
            and re.search(r"\\u[0-9a-fA-F]{0,3}$", candidate) is not None
        )
    if error.pos >= len(candidate):
        return True
    tail = candidate[error.pos :].strip()
    if any(literal.startswith(tail) and tail != literal for literal in ("true", "false", "null")):
        return True
    return re.fullmatch(r"(?:-|\d+\.|\d+(?:\.\d+)?[eE][+-]?)", tail) is not None


def parse_detail_variable_config_chunk(
    text: str,
    chunk_index: int,
    *,
    requirements: UserConfirmedRequirements,
    angle_inventory: Mapping[str, Any],
    prior_chunks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one chunk before any bounded transport or envelope recovery."""

    if not 1 <= chunk_index <= DETAIL_CHUNK_COUNT:
        raise ExecutorExecutionError("codex-dev 收到无效的详情图变量配置分段编号")
    candidate = text.strip()
    if "\ufffd" in candidate:
        raise DetailChunkTransportCorruption("detail chunk contains replacement character")
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        if _is_probable_json_truncation(candidate, error):
            raise DetailChunkTransportCorruption("detail chunk is not complete JSON") from None
        raise ExecutorExecutionError(
            "codex-dev 收到的详情图变量配置分段不是有效 JSON"
        ) from None
    if not isinstance(value, dict):
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段结构异常")
    if any(isinstance(item, str) and "\ufffd" in item for item in _walk_values(value)):
        raise DetailChunkTransportCorruption("detail chunk contains replacement character")

    _reject_unicode_damage_or_forbidden_keys(value, "详情图变量配置分段")

    if value.get("chunk_index") != chunk_index or value.get("chunk_count") != DETAIL_CHUNK_COUNT:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段编号异常")

    configs = value.get("configs")
    expected_ids = list(DETAIL_CONFIG_IDS_BY_CHUNK[chunk_index - 1])
    if not isinstance(configs, list) or len(configs) != 2:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段覆盖异常")
    actual_ids: list[str] = []
    for raw in configs:
        if not isinstance(raw, dict) or set(raw) != _VARIABLE_ALLOWED_CONFIG_FIELDS:
            raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段单项结构异常")
        if not isinstance(raw.get("per_image_overrides"), dict):
            raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段单项结构异常")
        actual_ids.append(str(raw.get("config_id") or ""))
    if actual_ids != expected_ids:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段覆盖异常")

    _validate_detail_chunk_business_content(
        value,
        chunk_index=chunk_index,
        requirements=requirements,
        angle_inventory=angle_inventory,
        prior_chunks=prior_chunks,
    )

    expected_keys = {"chunk_index", "chunk_count", "configs"}
    if chunk_index == 1:
        expected_keys.update(("common_constraints", "notes"))
    if chunk_index == DETAIL_CHUNK_COUNT:
        expected_keys.add("handheld_count_summary")
    if set(value) != expected_keys:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段结构异常")

    if chunk_index == 1:
        if not isinstance(value.get("notes"), str):
            raise DetailChunkEnvelopeCorrection(
                detail_chunk_business_fingerprint(value, chunk_index)
            )
    return value


def assemble_detail_variable_config_chunks(
    chunks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild the original full response in memory after every chunk passes."""

    if len(chunks) != DETAIL_CHUNK_COUNT:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段数量异常")
    configs = [config for chunk in chunks for config in chunk["configs"]]
    if [str(config.get("config_id") or "") for config in configs] != [
        f"detail_{index:02d}" for index in range(1, 9)
    ]:
        raise ExecutorExecutionError("codex-dev 收到的详情图变量配置分段覆盖异常")
    return {
        "common_constraints": dict(chunks[0]["common_constraints"]),
        "configs": [dict(config) for config in configs],
        "handheld_count_summary": dict(chunks[-1]["handheld_count_summary"]),
        "notes": str(chunks[0]["notes"]),
    }


def _validate_bound_angle(
    binding: str,
    qualified: Mapping[str, Mapping[str, Any]],
    label: str,
) -> None:
    matches = [asset_id for asset_id in qualified if asset_id in binding]
    if len(matches) != 1:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}角度绑定异常")
    record = qualified[matches[0]]
    slot = str(record.get("angle_slot") or "")
    if slot not in {"A", "B", "C"} or not re.search(rf"(?:{slot}\s*槽位|槽位\s*{slot})", binding):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}角度绑定异常")
    if re.search(r"(?:D\s*槽位|槽位\s*D)", binding):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}使用了缺失的 D 槽位")


def _resolve_bound_angle_literal(
    binding: str,
    qualified: Mapping[str, Mapping[str, Any]],
    label: str,
) -> tuple[str, str]:
    """Return the unique source asset and A/B/C slot after existing validation."""

    _validate_bound_angle(binding, qualified, label)
    asset_id = next(asset_id for asset_id in qualified if asset_id in binding)
    slot = str(qualified[asset_id].get("angle_slot") or "")
    return asset_id, slot


def _validate_detail_chunk_business_content(
    value: Mapping[str, Any],
    *,
    chunk_index: int,
    requirements: UserConfirmedRequirements,
    angle_inventory: Mapping[str, Any],
    prior_chunks: Sequence[Mapping[str, Any]],
) -> None:
    """Prove config content is safe before granting one envelope correction."""

    label = "详情图变量配置"
    _reject_unsupported_claims(
        value,
        requirements.height_cm,
        label,
        product_type=requirements.product_type,
        lexicons=_requirements_recipe(requirements).lexicons,
        confirmed_dimensions=_confirmed_dimensions(requirements),
        defer_style_master_prop_materials=True,
    )
    _reject_scene_policy_violations(value, requirements, label)
    if chunk_index == 1 and (
        not isinstance(value.get("common_constraints"), dict)
        or not value["common_constraints"]
    ):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}数量或结构异常")
    qualified = qualified_angle_assets(angle_inventory)
    enabled_handheld = 0
    start_index = (chunk_index - 1) * 2
    for offset, raw in enumerate(value["configs"]):
        overrides = raw["per_image_overrides"]
        if set(overrides) != set(DETAIL_REQUIRED_OVERRIDE_FIELDS):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}缺少规范字段")
        if any(
            not isinstance(overrides[field], str) or not overrides[field].strip()
            for field in DETAIL_REQUIRED_OVERRIDE_FIELDS
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}字段内容异常")
        _validate_bound_angle(overrides["绑定角度槽位"], qualified, label)
        if overrides["输出画布比例"].strip() != "3:4":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}画布比例异常")
        if f"约 {requirements.height_cm} 厘米" not in overrides["尺寸比例锁定"]:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}缺少已确认高度")

        handheld_declaration = overrides["手持交互声明"]
        handheld = "本张图不启用手持场景" not in handheld_declaration
        enabled_handheld += int(handheld)
        dynamic_reference = overrides["动态手持样式参考图调用"].strip()
        if not handheld and dynamic_reference != "无":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and "静态握持" in handheld_declaration and dynamic_reference != "无，仅动态拿起场景可调用":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and "动态拿起" in handheld_declaration and dynamic_reference != "未提供，不调用":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and not any(kind in handheld_declaration for kind in ("静态握持", "动态拿起")):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")

        config_index = start_index + offset
        expected_module = f"模块{config_index + 1:02d}"
        module_assignment = overrides["标准模块归属"].strip()
        if not re.fullmatch(rf"{re.escape(expected_module)}(?:\s+.+)?", module_assignment):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}模块覆盖异常")
        if config_index == 4:
            size_info = overrides["尺寸标注信息"]
            size_rule = overrides["尺寸标注图规则"]
            if handheld or f"高度约 {requirements.height_cm} 厘米" not in size_info:
                raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
            if any(term not in size_info for term in ("禁止", "容量", "宽度", "直径", "重量", "材质")):
                raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
            if f"高度约 {requirements.height_cm} 厘米" not in size_rule:
                raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
        elif (
            "非尺寸标注图" not in overrides["尺寸标注信息"]
            or "非尺寸标注图" not in overrides["尺寸标注图规则"]
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}尺寸标注范围异常")

    if enabled_handheld > requirements.handheld_detail:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量异常")

    if chunk_index == DETAIL_CHUNK_COUNT:
        all_configs = [
            raw
            for chunk in (*prior_chunks, value)
            for raw in chunk["configs"]
        ]
        if len(all_configs) != 8:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}分段覆盖异常")
        enabled_ids = [
            str(raw.get("config_id") or "")
            for raw in all_configs
            if "本张图不启用手持场景"
            not in raw["per_image_overrides"]["手持交互声明"]
        ]
        if len(enabled_ids) != requirements.handheld_detail:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量异常")
        summary = value.get("handheld_count_summary")
        if not isinstance(summary, dict):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量说明异常")
        _validate_handheld_summary(
            summary,
            mode="detail",
            expected_handheld=requirements.handheld_detail,
            expected_count=8,
            enabled_ids=enabled_ids,
            label=label,
        )


def detail_chunk_business_fingerprint(
    value: Mapping[str, Any],
    chunk_index: int,
) -> str:
    """Fingerprint business-bearing fields while excluding the wrapper-only notes field."""

    payload: dict[str, Any] = {"configs": value["configs"]}
    if chunk_index == 1:
        payload["common_constraints"] = value["common_constraints"]
    if chunk_index == DETAIL_CHUNK_COUNT:
        payload["handheld_count_summary"] = value["handheld_count_summary"]
    return stable_json_sha256(payload)


def _validate_handheld_summary(
    summary: Mapping[str, Any],
    *,
    mode: str,
    expected_handheld: int,
    expected_count: int,
    enabled_ids: list[str],
    label: str,
) -> None:
    english_valid = (
        summary.get("requested") == expected_handheld
        and summary.get("enabled") == expected_handheld
        and summary.get("disabled") == expected_count - expected_handheld
        and summary.get("fully_satisfied") is True
    )
    scope = "主图" if mode == "main" else "详情图"
    chinese_valid = (
        summary.get(f"用户要求{scope}手持数量") == expected_handheld
        and summary.get("实际启用手持数量") == expected_handheld
        and summary.get("未启用手持数量") == expected_count - expected_handheld
        and summary.get("启用手持配置") == enabled_ids
        and summary.get("是否完全满足用户数量") in {"是", True}
    )
    if not english_valid and not chinese_valid:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量说明异常")


def parse_variable_config_response(
    text: str,
    *,
    mode: str,
    product_id: str,
    requirements: UserConfirmedRequirements,
    angle_inventory: Mapping[str, Any],
    upstream_paths: Mapping[str, Path],
) -> dict[str, Any]:
    """Validate a model-owned variable plan and inject trusted envelope fields."""

    if mode not in {"main", "detail"}:
        raise ExecutorExecutionError("codex-dev 收到不支持的变量配置模式")
    is_main = mode == "main"
    label = "主图变量配置" if is_main else "详情图变量配置"
    required_upstreams = (
        ("product_identity_archive", "style_master", "angle_inventory")
        if is_main
        else (
            "product_identity_archive",
            "style_master",
            "angle_inventory",
            "main_variable_configs",
        )
    )
    if set(upstream_paths) != set(required_upstreams):
        raise ExecutorExecutionError(f"codex-dev 无法固定{label}上游引用")
    style_master_text = _load_style_master_material_reference_text(
        upstream_paths["style_master"],
        product_id=product_id,
    )
    value = _extract_json_object(text, label)
    if set(value) - _VARIABLE_ALLOWED_TOP_LEVEL:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}包含越界顶层字段")
    _reject_unicode_damage_or_forbidden_keys(value, label)
    _reject_unsupported_claims(
        value,
        requirements.height_cm,
        label,
        product_type=requirements.product_type,
        lexicons=_requirements_recipe(requirements).lexicons,
        confirmed_dimensions=_confirmed_dimensions(requirements),
        style_master_text=style_master_text,
    )
    _reject_scene_policy_violations(value, requirements, label)

    common = value.get("common_constraints")
    configs = value.get("configs")
    summary = value.get("handheld_count_summary")
    expected_count = 6 if is_main else 8
    if (
        not isinstance(common, dict)
        or not common
        or not isinstance(configs, list)
        or len(configs) != expected_count
    ):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}数量或结构异常")
    if not isinstance(summary, dict):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量说明异常")

    prefix = "main" if is_main else "detail"
    expected_ids = [f"{prefix}_{index:02d}" for index in range(1, expected_count + 1)]
    required_fields = MAIN_REQUIRED_OVERRIDE_FIELDS if is_main else DETAIL_REQUIRED_OVERRIDE_FIELDS
    qualified = qualified_angle_assets(angle_inventory)
    normalized_configs: list[dict[str, Any]] = []
    enabled_handheld = 0
    enabled_handheld_ids: list[str] = []
    for index, raw in enumerate(configs):
        if not isinstance(raw, dict) or set(raw) - _VARIABLE_ALLOWED_CONFIG_FIELDS:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}单项结构异常")
        if raw.get("config_id") != expected_ids[index]:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}编号异常")
        overrides = raw.get("per_image_overrides")
        if not isinstance(overrides, dict) or set(overrides) != set(required_fields):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}缺少规范字段")
        if any(not isinstance(overrides[field], str) or not overrides[field].strip() for field in required_fields):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}字段内容异常")
        _validate_bound_angle(overrides["绑定角度槽位"], qualified, label)
        expected_ratio = "1:1" if is_main else "3:4"
        if overrides["输出画布比例"].strip() != expected_ratio:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}画布比例异常")
        if f"约 {requirements.height_cm} 厘米" not in overrides["尺寸比例锁定"]:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}缺少已确认高度")
        handheld = "本张图不启用手持场景" not in overrides["手持交互声明"]
        enabled_handheld += int(handheld)
        if handheld:
            enabled_handheld_ids.append(expected_ids[index])
        handheld_declaration = overrides["手持交互声明"]
        dynamic_reference = overrides["动态手持样式参考图调用"].strip()
        if not handheld and dynamic_reference != "无":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and "静态握持" in handheld_declaration and dynamic_reference != "无，仅动态拿起场景可调用":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and "动态拿起" in handheld_declaration and dynamic_reference != "未提供，不调用":
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if handheld and not any(kind in handheld_declaration for kind in ("静态握持", "动态拿起")):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}手持规则调用异常")
        if not is_main:
            expected_module = f"模块{index + 1:02d}"
            module_assignment = overrides["标准模块归属"].strip()
            if not re.fullmatch(rf"{re.escape(expected_module)}(?:\s+.+)?", module_assignment):
                raise ExecutorExecutionError(f"codex-dev 收到的{label}模块覆盖异常")
            if index == 4:
                size_info = overrides["尺寸标注信息"]
                size_rule = overrides["尺寸标注图规则"]
                if handheld or f"高度约 {requirements.height_cm} 厘米" not in size_info:
                    raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
                if any(term not in size_info for term in ("禁止", "容量", "宽度", "直径", "重量", "材质")):
                    raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
                if f"高度约 {requirements.height_cm} 厘米" not in size_rule:
                    raise ExecutorExecutionError(f"codex-dev 收到的{label}模块05规则异常")
            else:
                if "非尺寸标注图" not in overrides["尺寸标注信息"] or "非尺寸标注图" not in overrides["尺寸标注图规则"]:
                    raise ExecutorExecutionError(f"codex-dev 收到的{label}尺寸标注范围异常")

        resolved = dict(common)
        resolved.update(overrides)
        normalized_configs.append(
            {
                "config_id": expected_ids[index],
                "output_type": mode,
                "per_image_overrides": dict(overrides),
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": str(raw.get("notes") or ""),
            }
        )

    expected_handheld = requirements.handheld_main if is_main else requirements.handheld_detail
    if enabled_handheld != expected_handheld:
        raise ExecutorExecutionError(f"codex-dev 收到的{label}手持数量异常")
    _validate_handheld_summary(
        summary,
        mode=mode,
        expected_handheld=expected_handheld,
        expected_count=expected_count,
        enabled_ids=enabled_handheld_ids,
        label=label,
    )

    return {
        "product_id": product_id,
        "artifact_type": f"{mode}_variable_config",
        "config_count": expected_count,
        "upstream_artifacts": {key: str(upstream_paths[key]) for key in required_upstreams},
        "common_constraints": dict(common),
        "configs": normalized_configs,
        "notes": str(value.get("notes") or ""),
    }


def build_final_prompt_batch_prompt(
    *,
    mode: str,
    product_id: str,
    repository_root: Path,
    identity: Mapping[str, Any],
    style_master: Mapping[str, Any],
    angle_inventory: Mapping[str, Any],
    variable_config: Mapping[str, Any],
    requirements: UserConfirmedRequirements,
) -> str:
    """Build one self-contained prompt-only compilation turn."""

    if mode not in {"main", "detail"}:
        raise ExecutorExecutionError("codex-dev 收到不支持的最终提示词模式")
    recipe = _requirements_recipe(requirements)
    skill_text = _load_skill_text(repository_root, "final-prompt-compiler", "最终提示词")
    runtime = load_skill_runtime_package(
        repository_root,
        "final-prompt-compiler",
        "runtime_rule_slices/final-prompt-compiler.runtime_rule_slices.json",
        "最终提示词",
        recipe,
    )
    _validate_missing_d_confirmation(angle_inventory, requirements)
    qualified = qualified_angle_assets(angle_inventory)
    allowed_angles = [
        qualified[key]
        for key in sorted(qualified)
        if str(qualified[key].get("angle_slot") or "").strip() in {"A", "B", "C"}
    ]
    configs = _validate_variable_config_document(
        variable_config,
        mode=mode,
        product_id=product_id,
    )
    expected_ids = [str(config["config_id"]) for config in configs]
    expected_ratio = "1:1" if mode == "main" else "3:4"
    expected_handheld = requirements.handheld_main if mode == "main" else requirements.handheld_detail
    handheld_contract_lines: list[str] = []
    binding_contract_lines: list[str] = []
    for config in configs:
        config_id = str(config["config_id"])
        overrides = config["per_image_overrides"]
        handheld_declaration = str(overrides.get("手持交互声明") or "")
        if "本张图不启用手持场景" in handheld_declaration:
            handheld_contract_lines.append(
                f"- {config_id}：final_prompt 正文必须原样出现完整否定短语"
                "“本张图不启用手持场景”。"
            )
        else:
            handheld_contract_lines.append(
                f"- {config_id}：final_prompt 正文必须原样出现完整肯定短语“启用手持场景”，"
                "且该正文不得出现完整否定短语“本张图不启用手持场景”。"
            )

        binding = str(overrides.get("绑定角度槽位") or "")
        asset_id, slot = _resolve_bound_angle_literal(binding, qualified, "最终提示词编译")
        binding_contract_lines.append(
            f"- {config_id}：final_prompt 正文必须原样出现源图编号“{asset_id}”，"
            f"并且必须原样出现“{slot} 槽位”或“槽位 {slot}”中的至少一种。"
        )

    all_angle_assets = {
        str(item.get("source_asset_id") or "").strip()
        for item in angle_inventory.get("angle_slots", [])
        if isinstance(item, Mapping)
    }
    rejected_assets = sorted(
        asset_id for asset_id in all_angle_assets if asset_id and asset_id not in qualified
    )
    if rejected_assets:
        forbidden_angle_contract = (
            "全批每一份 final_prompt 正文均不得出现以下任何被拒源图编号："
            f"{json.dumps(rejected_assets, ensure_ascii=False)}；"
            "也不得出现“D 槽位”或“槽位 D”。"
        )
    else:
        forbidden_angle_contract = (
            "全批每一份 final_prompt 正文均不得出现“D 槽位”或“槽位 D”；"
            "本次角度表没有需要另列的被拒源图编号。"
        )
    literal_contract = "\n".join(
        (
            "【逐编号字面契约】",
            "以下每条只允许由同一 config_id 的 final_prompt 正文满足；negative_prompt、"
            "其他配置正文、【正式变量配置】或其他上游内容中出现相同字样，均不算满足。",
            "",
            "【手持状态】",
            *handheld_contract_lines,
            "",
            "注意：“本张图不启用手持场景”包含“启用手持场景”作为子串。"
            "对启用手持的配置，完整否定短语一旦出现即为不合格，"
            "不能用其中的肯定子串充当肯定要求。",
            "",
            "【角度绑定】",
            *binding_contract_lines,
            "",
            forbidden_angle_contract,
        )
    )
    facts = {
        "product_type": requirements.product_type,
        "height": f"约 {requirements.height_cm} 厘米",
        "expected_handheld": expected_handheld,
        "allow_clear_water": requirements.allow_clear_water,
        "forbid_pouring_and_heating": requirements.forbid_pouring_and_heating,
        "missing_d_no_retake": requirements.missing_d_no_retake,
        "unconfirmed": [
            "容量",
            "宽度",
            "直径",
            "重量",
            "具体材质",
            "耐热性能",
            "认证",
            "品牌",
            "型号",
        ],
    }
    if requirements.length_cm is not None:
        facts["length"] = f"约 {requirements.length_cm} 厘米"
    if requirements.width_cm is not None:
        facts["width"] = f"约 {requirements.width_cm} 厘米"
    category_prompt = recipe.render_prompt(
        "final_prompt",
        **_category_prompt_values(
            requirements,
            expected_ratio=expected_ratio,
            handheld_count=expected_handheld,
        ),
    ).rstrip("\r\n")
    scene_safety_collective_rule = _category_prompt_values(
        requirements,
    )["scene_safety_collective_rule"]
    return f"""你正在为单品批次 {product_id} 编译 {mode} 配置的最终提示词，只处理 final_prompts 的这一批。
这是提示词编译，不生成图片、不生成 ComfyUI 作业、不执行 QC，也不处理套装。
必须且只返回这些配置：{json.dumps(expected_ids, ensure_ascii=False)}。
{category_prompt}
final_prompt 正文必须遵守以下场景安全规则：{scene_safety_collective_rule}
场景规则句（如需复述必须原样）：{_variable_scene_rule(requirements)}
如需逐词列出禁止项，只能写入 negative_prompt 字段；不得把逐词禁止清单写入 final_prompt 正文。
恰好 {expected_handheld} 份保持启用手持；{_final_forbidden_rule(requirements)}
不得新增变量配置没有的道具、文字、卖点或页面任务，不得把 Skill 或运行规则正文复制成最终画面要求。
只返回一个 JSON 对象，形状必须严格为 {{"prompts":[{{"config_id":"...","final_prompt":"...","negative_prompt":"..."}}]}}，不要 Markdown、说明、上游路径、图片、QC 或其他字段。

{literal_contract}

【Skill 原文】
{skill_text}

【运行规则包】
{json.dumps(runtime, ensure_ascii=False, sort_keys=True)}

【用户确认事实】
{json.dumps(facts, ensure_ascii=False, sort_keys=True)}

【产品身份档案】
{json.dumps(identity, ensure_ascii=False, sort_keys=True)}

【风格母版】
{json.dumps(style_master, ensure_ascii=False, sort_keys=True)}

【正式变量配置】
{json.dumps(variable_config, ensure_ascii=False, sort_keys=True)}

【仅允许绑定的角度记录】
{json.dumps(allowed_angles, ensure_ascii=False, sort_keys=True)}
"""


def _validate_variable_config_document(
    document: Mapping[str, Any],
    *,
    mode: str,
    product_id: str,
) -> list[dict[str, Any]]:
    expected_count = 6 if mode == "main" else 8
    expected_ids = [f"{mode}_{index:02d}" for index in range(1, expected_count + 1)]
    if (
        document.get("product_id") != product_id
        or document.get("artifact_type") != f"{mode}_variable_config"
        or document.get("config_count") != expected_count
        or not isinstance(document.get("common_constraints"), dict)
        or not isinstance(document.get("configs"), list)
        or len(document["configs"]) != expected_count
    ):
        raise ExecutorExecutionError("codex-dev 检测到正式变量配置结构异常")
    common = document["common_constraints"]
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(document["configs"]):
        if (
            not isinstance(raw, dict)
            or raw.get("config_id") != expected_ids[index]
            or raw.get("output_type") != mode
            or not isinstance(raw.get("per_image_overrides"), dict)
        ):
            raise ExecutorExecutionError("codex-dev 检测到正式变量配置单项异常")
        resolved = dict(common)
        resolved.update(raw["per_image_overrides"])
        if raw.get("resolved_variable_config_sha256") != stable_json_sha256(resolved):
            raise ExecutorExecutionError("codex-dev 检测到正式变量配置指纹异常")
        result.append(dict(raw))
    return result


def parse_final_prompt_batch_response(
    text: str,
    *,
    mode: str,
    product_id: str,
    requirements: UserConfirmedRequirements,
    angle_inventory: Mapping[str, Any],
    variable_config: Mapping[str, Any],
    style_master_text: str | None = None,
) -> dict[str, dict[str, str]]:
    """Validate one prompt batch against immutable variable-config decisions."""

    if mode not in {"main", "detail"}:
        raise ExecutorExecutionError("codex-dev 收到不支持的最终提示词模式")
    label = "主图最终提示词" if mode == "main" else "详情图最终提示词"
    value = _extract_json_object(text, label)
    if set(value) != {"prompts"} or not isinstance(value["prompts"], list):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}返回格式异常")
    if any(isinstance(item, str) and "\ufffd" in item for item in _walk_values(value)):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}包含损坏字符")
    _reject_unsupported_claims(
        value,
        requirements.height_cm,
        label,
        product_type=requirements.product_type,
        lexicons=_requirements_recipe(requirements).lexicons,
        confirmed_dimensions=_confirmed_dimensions(requirements),
        style_master_text=style_master_text,
    )
    _reject_scene_policy_violations(value, requirements, label)

    configs = _validate_variable_config_document(variable_config, mode=mode, product_id=product_id)
    expected_ids = [config["config_id"] for config in configs]
    if len(value["prompts"]) != len(expected_ids):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}数量异常")
    qualified = qualified_angle_assets(angle_inventory)
    all_angle_assets = {
        str(item.get("source_asset_id") or "").strip()
        for item in angle_inventory.get("angle_slots", [])
        if isinstance(item, Mapping)
    }
    rejected_assets = {asset_id for asset_id in all_angle_assets if asset_id and asset_id not in qualified}
    expected_ratio = "1:1" if mode == "main" else "3:4"
    parsed: dict[str, dict[str, str]] = {}
    enabled = 0
    for index, raw in enumerate(value["prompts"]):
        if not isinstance(raw, dict) or set(raw) != {"config_id", "final_prompt", "negative_prompt"}:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}单项结构异常")
        config_id = raw.get("config_id")
        final_prompt = raw.get("final_prompt")
        negative_prompt = raw.get("negative_prompt")
        if (
            config_id != expected_ids[index]
            or not isinstance(final_prompt, str)
            or not final_prompt.strip()
            or not isinstance(negative_prompt, str)
            or not negative_prompt.strip()
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}单项内容异常")
        overrides = configs[index]["per_image_overrides"]
        binding = str(overrides.get("绑定角度槽位") or "")
        _validate_bound_angle(binding, qualified, label)
        bound_assets = [asset_id for asset_id in qualified if asset_id in binding]
        bound_asset = bound_assets[0]
        slot = str(qualified[bound_asset].get("angle_slot") or "")
        if bound_asset not in final_prompt or not re.search(
            rf"(?:{slot}\s*槽位|槽位\s*{slot})", final_prompt
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}未保留角度绑定")
        if any(asset_id in final_prompt for asset_id in rejected_assets) or re.search(
            r"(?:D\s*槽位|槽位\s*D)", final_prompt
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}使用了禁止角度")
        if expected_ratio not in final_prompt:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}未保留画布比例")
        if f"约 {requirements.height_cm} 厘米" not in final_prompt:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}未保留已确认高度")
        handheld = "本张图不启用手持场景" not in str(overrides.get("手持交互声明") or "")
        enabled += int(handheld)
        if handheld and (
            "本张图不启用手持场景" in final_prompt or "启用手持场景" not in final_prompt
        ):
            raise ExecutorExecutionError(f"codex-dev 收到的{label}未保留手持状态")
        if not handheld and "本张图不启用手持场景" not in final_prompt:
            raise ExecutorExecutionError(f"codex-dev 收到的{label}未保留手持状态")
        parsed[str(config_id)] = {
            "final_prompt": final_prompt.strip(),
            "negative_prompt": negative_prompt.strip(),
        }
    expected_handheld = requirements.handheld_main if mode == "main" else requirements.handheld_detail
    if enabled != expected_handheld:
        raise ExecutorExecutionError(f"codex-dev 检测到{label}上游手持数量异常")
    return parsed


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ExecutorExecutionError("codex-dev 无法固定变量配置文件指纹") from None
    return digest.hexdigest()


def final_prompt_bundle_targets(output_dir: Path) -> tuple[Path, ...]:
    ids = [f"main_{index:02d}" for index in range(1, 7)] + [
        f"detail_{index:02d}" for index in range(1, 9)
    ]
    targets: list[Path] = []
    for config_id in ids:
        targets.extend(
            (
                output_dir / f"{config_id}_final_prompt.json",
                output_dir / f"{config_id}_final_prompt.md",
            )
        )
    targets.extend((output_dir / "final_prompt_index.json", output_dir / "final_prompt_index.md"))
    return tuple(targets)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _markdown_bytes(title: str, value: Mapping[str, Any]) -> bytes:
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return f"# {title}\n\n```json\n{body}\n```\n".encode("utf-8")


def build_final_prompt_bundle(
    *,
    product_id: str,
    output_dir: Path,
    prompt_batches: Mapping[str, Mapping[str, Mapping[str, str]]],
    variable_configs: Mapping[str, tuple[Mapping[str, Any], Path]],
    upstream_paths: Mapping[str, Path],
    angle_inventory: Mapping[str, Any],
) -> dict[Path, bytes]:
    """Build all prompt-only artifacts in memory before the exclusive commit."""

    if set(prompt_batches) != {"main", "detail"} or set(variable_configs) != {"main", "detail"}:
        raise ExecutorExecutionError("codex-dev 无法构建完整的最终提示词批次")
    required_upstreams = {"product_identity_archive", "style_master", "angle_inventory"}
    if set(upstream_paths) != required_upstreams:
        raise ExecutorExecutionError("codex-dev 无法固定最终提示词上游引用")
    image_lookup = {
        str(item.get("asset_id") or ""): str(item.get("file_path") or "")
        for item in angle_inventory.get("image_assets", [])
        if isinstance(item, Mapping) and item.get("asset_id") and item.get("file_path")
    }
    qualified = qualified_angle_assets(angle_inventory)
    files: dict[Path, bytes] = {}
    index_items: list[dict[str, Any]] = []
    for mode in ("main", "detail"):
        document, source_path = variable_configs[mode]
        configs = _validate_variable_config_document(document, mode=mode, product_id=product_id)
        source_sha256 = _file_sha256(source_path)
        prompts = prompt_batches[mode]
        if set(prompts) != {config["config_id"] for config in configs}:
            raise ExecutorExecutionError("codex-dev 无法构建完整的最终提示词批次")
        for index, config in enumerate(configs):
            config_id = config["config_id"]
            binding = str(config["per_image_overrides"].get("绑定角度槽位") or "")
            bound_assets = [asset_id for asset_id in qualified if asset_id in binding]
            if len(bound_assets) != 1:
                raise ExecutorExecutionError("codex-dev 无法固定最终提示词角度引用")
            bound_asset = bound_assets[0]
            final_doc = {
                "product_id": product_id,
                "artifact_type": "final_prompt",
                "upstream_artifacts": {
                    "product_identity_archive": str(upstream_paths["product_identity_archive"]),
                    "style_master": str(upstream_paths["style_master"]),
                    "angle_inventory": str(upstream_paths["angle_inventory"]),
                    "variable_config": str(source_path),
                },
                "variable_config": {
                    "config_id": config_id,
                    "output_type": mode,
                    "source_path": str(source_path),
                    "source_sha256": source_sha256,
                    "source_schema": "common_constraints + per_image_overrides",
                    "common_constraints_ref": {
                        "path": str(source_path),
                        "json_pointer": "/common_constraints",
                    },
                    "per_image_overrides_ref": {
                        "path": str(source_path),
                        "json_pointer": f"/configs/{index}/per_image_overrides",
                    },
                    "resolved_variable_config_sha256": config[
                        "resolved_variable_config_sha256"
                    ],
                },
                "uses_upstream_prompt_files_as_visual_requirements": False,
                "final_prompt": prompts[config_id]["final_prompt"],
                "negative_prompt": prompts[config_id]["negative_prompt"],
                "notes": "仅由已验收上游档案和本张变量配置编译；本阶段未生成图片、ComfyUI 作业或 QC 产物。",
            }
            json_path = output_dir / f"{config_id}_final_prompt.json"
            md_path = output_dir / f"{config_id}_final_prompt.md"
            files[json_path] = _json_bytes(final_doc)
            files[md_path] = _markdown_bytes(f"{config_id} Final Prompt", final_doc)
            index_items.append(
                {
                    "config_id": config_id,
                    "output_type": mode,
                    "final_prompt_path": str(json_path),
                    "bound_reference": image_lookup.get(bound_asset, bound_asset),
                }
            )
    index = {
        "product_id": product_id,
        "artifact_type": "final_prompt_index",
        "prompt_count": len(index_items),
        "uses_upstream_prompt_files_as_visual_requirements": False,
        "items": index_items,
        "notes": "提示词专用索引；未生成 ComfyUI、QC 或图片产物。",
    }
    files[output_dir / "final_prompt_index.json"] = _json_bytes(index)
    files[output_dir / "final_prompt_index.md"] = _markdown_bytes("Final Prompt Index", index)
    if set(files) != set(final_prompt_bundle_targets(output_dir)):
        raise ExecutorExecutionError("codex-dev 无法构建完整的最终提示词批次")
    return files


def _temporary_bytes(target: Path, content: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_bundle_exclusive(files: Mapping[Path, bytes], label: str) -> tuple[Path, ...]:
    """Commit a prebuilt bundle without overwriting or leaving partial outputs."""

    targets = tuple(Path(path) for path in files)
    if not targets or len(set(targets)) != len(targets):
        raise ExecutorExecutionError(f"codex-dev 无法写入有效的{label}")
    if any(path.exists() for path in targets):
        raise ExecutorExecutionError(f"正式{label}已存在，codex-dev 不会覆盖")

    temporary: dict[Path, Path] = {}
    created: list[Path] = []
    try:
        for target in targets:
            content = files[target]
            if not isinstance(content, bytes):
                raise TypeError("bundle content must be bytes")
            temporary[target] = _temporary_bytes(target, content)
        for target in targets:
            target.hardlink_to(temporary[target])
            created.append(target)
        return targets
    except BaseException as exc:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        if isinstance(exc, ExecutorExecutionError):
            raise
        raise ExecutorExecutionError(f"codex-dev 写入{label}失败") from None
    finally:
        for temp_path in temporary.values():
            temp_path.unlink(missing_ok=True)


def write_json_exclusive(path: Path, value: Mapping[str, Any], label: str) -> Path:
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    write_bundle_exclusive({Path(path): content}, label)
    return Path(path)
