"""Validated, fail-closed loading for category-owned production recipes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from image_count_contract import ImageCountContractError, image_count_spec


DEFAULT_CATEGORY_KEY = "杯类"
DIMENSION_KEYS = ("length_cm", "width_cm", "height_cm")
ADVANCED_OPTION_KEYS = (
    "forbid_pouring_and_heating",
    "missing_d_no_retake",
)
RECIPE_FILE_KEYS = (
    "form",
    "lexicons",
    "identity_prompt",
    "style_prompt",
    "angle_prompt",
    "angle_boundary",
    "main_prompt",
    "detail_prompt",
    "final_prompt",
    "main_runtime",
    "detail_runtime",
    "final_runtime",
    "qc_runtime",
    "qc_checklist",
    "qc_workflow",
    "qc_realism",
)
RUNTIME_SKILLS = {
    "main_runtime": "main-variable-config",
    "detail_runtime": "detail-variable-config",
    "final_runtime": "final-prompt-compiler",
    "qc_runtime": "qc-inspector",
}


class CategoryRecipeError(ValueError):
    """A category recipe is absent, unsafe, incomplete, or malformed."""


@dataclass(frozen=True)
class CategoryRecipe:
    key: str
    display_name: str
    product_noun: str
    business_review_status: str
    form: Mapping[str, Any]
    lexicons: Mapping[str, Any]
    prompts: Mapping[str, str]
    runtime_packages: Mapping[str, Mapping[str, Any]]
    qc_documents: Mapping[str, str]
    content_sha256: str

    def render_prompt(self, name: str, **values: object) -> str:
        try:
            template = self.prompts[name]
            return template.format_map(values)
        except (KeyError, ValueError):
            raise CategoryRecipeError(
                f"品类“{self.display_name}”的{name}提示词模板不完整"
            ) from None


def _recipe_root(repository_root: Path) -> Path:
    return repository_root.resolve() / "categories"


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CategoryRecipeError(f"无法读取有效的{label}") from None
    if not isinstance(value, dict):
        raise CategoryRecipeError(f"无法读取有效的{label}")
    return value


def _resolved_recipe_file(category_root: Path, relative_value: object, label: str) -> Path:
    if not isinstance(relative_value, str) or not relative_value.strip():
        raise CategoryRecipeError(f"品类配方缺少{label}")
    relative_path = Path(relative_value)
    if relative_path.is_absolute():
        raise CategoryRecipeError(f"品类配方的{label}路径无效")
    try:
        resolved = (category_root / relative_path).resolve()
        if not resolved.is_relative_to(category_root.resolve()) or not resolved.is_file():
            raise CategoryRecipeError(f"品类配方的{label}文件无效")
    except (OSError, RuntimeError, ValueError):
        raise CategoryRecipeError(f"品类配方的{label}文件无效") from None
    return resolved


def _recipe_source_snapshot(
    repository_root: Path,
    category_key: str,
) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    key = str(category_key or "").strip()
    if not key or Path(key).name != key or key.startswith("_"):
        raise CategoryRecipeError("产品品类无效，请从已安装品类中重新选择")
    category_root = _recipe_root(repository_root) / key
    recipe_path = category_root / "recipe.json"
    recipe = _read_json(recipe_path, f"品类“{key}”配方")
    files = recipe.get("files")
    if not isinstance(files, dict) or set(files) != set(RECIPE_FILE_KEYS):
        raise CategoryRecipeError(f"品类“{key}”配方文件清单不完整")
    entries: list[tuple[str, bytes]] = [("recipe.json", recipe_path.read_bytes())]
    for name in RECIPE_FILE_KEYS:
        path = _resolved_recipe_file(category_root, files[name], name)
        try:
            entries.append((name, path.read_bytes()))
        except OSError:
            raise CategoryRecipeError(f"品类“{key}”的{name}文件无法读取") from None
    digest = hashlib.sha256()
    for name, payload in entries:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest(), tuple(entries)


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CategoryRecipeError(f"品类配方缺少{label}")
    return value.strip()


def _validate_form(form: dict[str, Any]) -> None:
    if set(form) != {"dimensions", "image_counts", "handheld", "advanced_options"}:
        raise CategoryRecipeError("品类表单元数据结构无效")
    try:
        count_specs = {
            mode: image_count_spec(form, mode)
            for mode in ("main", "detail")
        }
    except ImageCountContractError as error:
        raise CategoryRecipeError(str(error)) from None

    dimensions = form["dimensions"]
    if not isinstance(dimensions, dict) or set(dimensions) != {"required", "fields"}:
        raise CategoryRecipeError("品类尺寸元数据结构无效")
    required = dimensions["required"]
    fields = dimensions["fields"]
    if (
        not isinstance(required, list)
        or any(item not in DIMENSION_KEYS for item in required)
        or len(required) != len(set(required))
        or not isinstance(fields, list)
        or len(fields) != len(DIMENSION_KEYS)
    ):
        raise CategoryRecipeError("品类尺寸元数据无效")
    field_keys: list[str] = []
    for item in fields:
        if not isinstance(item, dict) or set(item) != {
            "key",
            "label",
            "unit",
            "minimum",
            "maximum",
        }:
            raise CategoryRecipeError("品类尺寸字段元数据无效")
        key = item["key"]
        minimum = item["minimum"]
        maximum = item["maximum"]
        if (
            key not in DIMENSION_KEYS
            or type(minimum) is not int
            or type(maximum) is not int
            or minimum <= 0
            or maximum < minimum
        ):
            raise CategoryRecipeError("品类尺寸字段范围无效")
        _nonempty_string(item["label"], "尺寸字段名称")
        _nonempty_string(item["unit"], "尺寸字段单位")
        field_keys.append(key)
    if tuple(field_keys) != DIMENSION_KEYS:
        raise CategoryRecipeError("品类尺寸字段必须依次声明长、宽、高")

    handheld = form["handheld"]
    if not isinstance(handheld, dict) or set(handheld) != {"main", "detail"}:
        raise CategoryRecipeError("品类手持元数据结构无效")
    for mode in ("main", "detail"):
        item = handheld[mode]
        if not isinstance(item, dict) or set(item) != {"default", "minimum"}:
            raise CategoryRecipeError("品类手持元数据无效")
        default = item["default"]
        minimum = item["minimum"]
        if (
            type(default) is not int
            or type(minimum) is not int
            or minimum < 0
            or not minimum <= default <= count_specs[mode].default
        ):
            raise CategoryRecipeError("品类手持默认值或范围无效")

    advanced = form["advanced_options"]
    if not isinstance(advanced, list) or len(advanced) != len(ADVANCED_OPTION_KEYS):
        raise CategoryRecipeError("品类高级选项元数据无效")
    for expected, item in zip(ADVANCED_OPTION_KEYS, advanced, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"field", "default", "label", "description"}
            or item["field"] != expected
            or type(item["default"]) is not bool
        ):
            raise CategoryRecipeError("品类高级选项元数据无效")
        _nonempty_string(item["label"], "高级选项名称")
        _nonempty_string(item["description"], "高级选项说明")


def _validate_lexicons(lexicons: dict[str, Any]) -> None:
    required_lists = {
        "product_subject_terms",
        "product_material_context_markers",
        "protected_structure_terms",
        "ambiguous_product_or_prop_terms",
        "unsupported_fact_terms",
        "material_prompt_examples",
        "scene_content_terms",
        "prohibited_action_terms",
        "final_forbidden_action_terms",
        "competing_dimension_terms",
    }
    if set(lexicons) != required_lists | {
        "handheld_phrase",
        "optional_dimension_prompts",
        "scene_rules",
    }:
        raise CategoryRecipeError("品类词表结构无效")
    for name in required_lists:
        value = lexicons[name]
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise CategoryRecipeError(f"品类词表{name}无效")
    _nonempty_string(lexicons["handheld_phrase"], "手持动作短语")
    optional_dimension_prompts = lexicons["optional_dimension_prompts"]
    if (
        not isinstance(optional_dimension_prompts, dict)
        or set(optional_dimension_prompts) != {"main", "detail", "final"}
        or any(not isinstance(value, str) for value in optional_dimension_prompts.values())
    ):
        raise CategoryRecipeError("品类可选尺寸提示词结构无效")
    scene_rules = lexicons["scene_rules"]
    expected_scene_rules = {
        "no_water_forbid_actions",
        "water_forbid_actions",
        "no_water_allow_actions",
        "water_allow_actions",
    }
    if not isinstance(scene_rules, dict) or set(scene_rules) != expected_scene_rules:
        raise CategoryRecipeError("品类场景规则结构无效")
    for value in scene_rules.values():
        _nonempty_string(value, "场景规则")


def _validate_runtime(value: dict[str, Any], expected_skill: str) -> None:
    slices = value.get("slices")
    if (
        value.get("artifact_type") != "runtime_rule_slice_package"
        or value.get("skill") != expected_skill
        or not isinstance(slices, list)
        or not slices
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("text"), str)
            or not str(item["text"]).strip()
            for item in slices
        )
    ):
        raise CategoryRecipeError(f"品类{expected_skill}运行规则无效")


@lru_cache(maxsize=32)
def _load_category_recipe_cached(
    root_text: str,
    category_key: str,
    content_sha256: str,
) -> CategoryRecipe:
    repository_root = Path(root_text)
    category_root = _recipe_root(repository_root) / category_key
    recipe = _read_json(category_root / "recipe.json", f"品类“{category_key}”配方")
    if set(recipe) != {
        "schema_version",
        "key",
        "display_name",
        "product_noun",
        "business_review_status",
        "files",
    }:
        raise CategoryRecipeError(f"品类“{category_key}”配方结构无效")
    if recipe["schema_version"] != 1 or recipe["key"] != category_key:
        raise CategoryRecipeError(f"品类“{category_key}”配方标识无效")
    display_name = _nonempty_string(recipe["display_name"], "显示名称")
    product_noun = _nonempty_string(recipe["product_noun"], "提示词产品名称")
    status = recipe["business_review_status"]
    if status not in {"approved", "pending_business_review"}:
        raise CategoryRecipeError(f"品类“{display_name}”业务复核状态无效")
    files = recipe["files"]

    def path_for(name: str) -> Path:
        return _resolved_recipe_file(category_root, files[name], name)

    form = _read_json(path_for("form"), f"品类“{display_name}”表单元数据")
    lexicons = _read_json(path_for("lexicons"), f"品类“{display_name}”词表")
    _validate_form(form)
    _validate_lexicons(lexicons)

    prompts: dict[str, str] = {}
    for name in (
        "identity_prompt",
        "style_prompt",
        "angle_prompt",
        "angle_boundary",
        "main_prompt",
        "detail_prompt",
        "final_prompt",
    ):
        try:
            text = path_for(name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CategoryRecipeError(f"品类“{display_name}”的{name}无法读取") from None
        if not text.strip():
            raise CategoryRecipeError(f"品类“{display_name}”的{name}为空")
        prompts[name] = text

    runtime_packages: dict[str, Mapping[str, Any]] = {}
    for name, expected_skill in RUNTIME_SKILLS.items():
        value = _read_json(path_for(name), f"品类“{display_name}”{expected_skill}运行规则")
        _validate_runtime(value, expected_skill)
        runtime_packages[name] = value

    qc_documents: dict[str, str] = {}
    for name in ("qc_checklist", "qc_workflow", "qc_realism"):
        try:
            text = path_for(name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CategoryRecipeError(f"品类“{display_name}”的{name}无法读取") from None
        if not text.strip():
            raise CategoryRecipeError(f"品类“{display_name}”的{name}为空")
        qc_documents[name] = text

    return CategoryRecipe(
        key=category_key,
        display_name=display_name,
        product_noun=product_noun,
        business_review_status=status,
        form=form,
        lexicons=lexicons,
        prompts=prompts,
        runtime_packages=runtime_packages,
        qc_documents=qc_documents,
        content_sha256=content_sha256,
    )


def load_category_recipe(repository_root: Path, category_key: str) -> CategoryRecipe:
    """Load one installed recipe and invalidate the cache when any recipe byte changes."""

    resolved_root = repository_root.resolve()
    digest, _entries = _recipe_source_snapshot(resolved_root, category_key)
    return _load_category_recipe_cached(str(resolved_root), str(category_key).strip(), digest)


def category_key_from_manifest(manifest: Mapping[str, Any]) -> str:
    """Keep old manifests compatible without mutating them."""

    raw = manifest.get("category", DEFAULT_CATEGORY_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise CategoryRecipeError("批次产品品类无效")
    return raw.strip()


def load_manifest_category(
    repository_root: Path,
    manifest: Mapping[str, Any],
) -> CategoryRecipe:
    return load_category_recipe(repository_root, category_key_from_manifest(manifest))


def installed_category_metadata(repository_root: Path) -> tuple[dict[str, Any], ...]:
    """Return public form metadata only; never expose paths or recipe digests."""

    root = _recipe_root(repository_root)
    try:
        keys = sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        )
    except OSError:
        raise CategoryRecipeError("无法读取已安装品类") from None
    if not keys:
        raise CategoryRecipeError("当前没有可用的产品品类")
    result: list[dict[str, Any]] = []
    for key in keys:
        recipe = load_category_recipe(repository_root, key)
        result.append(
            {
                "key": recipe.key,
                "display_name": recipe.display_name,
                "product_noun": recipe.product_noun,
                "form": recipe.form,
            }
        )
    return tuple(result)
