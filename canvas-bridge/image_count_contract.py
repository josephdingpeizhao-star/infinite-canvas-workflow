"""Canonical per-batch image-count, identifier, and wording helpers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import cycle, islice
from typing import Any, Mapping


SUPPORTED_MODES = ("main", "detail")
SUPPORTED_COUNT_MINIMUM = 1
SUPPORTED_COUNT_MAXIMUM = 30
DIMENSION_MODULE = 5
# 模块05“规格尺寸与容量”是唯一承担尺寸标注职责的详情模块。
_CHINESE_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
_NON_DIMENSION_MODULES = tuple(
    module for module in range(1, 9) if module != DIMENSION_MODULE
)
_DETAIL_EXTRA_MODULE_CYCLE = _NON_DIMENSION_MODULES
DETAIL_MODULE_LABELS = (
    "首屏 · 主视觉与卖点承接",
    "核心卖点证明",
    "使用场景与方法",
    "细节实拍与材质工艺",
    "规格尺寸与容量",
    "质感可信视觉呈现",
    "决策辅助与场景想象",
    "收尾氛围与风险克制",
)


class ImageCountContractError(ValueError):
    """The recipe or batch does not declare a supported image count."""


@dataclass(frozen=True)
class ImageCountSpec:
    default: int
    minimum: int
    maximum: int


def _supported_count(value: object) -> int:
    if (
        type(value) is not int
        or value < SUPPORTED_COUNT_MINIMUM
        or value > SUPPORTED_COUNT_MAXIMUM
    ):
        raise ImageCountContractError("图片张数必须是 1–30 的整数")
    return value


def image_count_spec(form: Mapping[str, Any], mode: str) -> ImageCountSpec:
    """Read one mode's defaults and bounds from category form metadata."""

    if mode not in SUPPORTED_MODES:
        raise ImageCountContractError("图片类型无效")
    image_counts = form.get("image_counts")
    raw = image_counts.get(mode) if isinstance(image_counts, Mapping) else None
    if not isinstance(raw, Mapping) or set(raw) != {"default", "minimum", "maximum"}:
        raise ImageCountContractError("品类张数元数据无效")
    default = raw.get("default")
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if (
        type(default) is not int
        or type(minimum) is not int
        or type(maximum) is not int
        or minimum != SUPPORTED_COUNT_MINIMUM
        or maximum != SUPPORTED_COUNT_MAXIMUM
        or not minimum <= default <= maximum
    ):
        raise ImageCountContractError("品类张数默认值或范围无效")
    return ImageCountSpec(default=default, minimum=minimum, maximum=maximum)


def default_image_counts(form: Mapping[str, Any]) -> tuple[int, int]:
    return (
        image_count_spec(form, "main").default,
        image_count_spec(form, "detail").default,
    )


def validate_image_count(value: object, form: Mapping[str, Any], mode: str) -> int:
    """Validate a batch value against the selected category's public metadata."""

    spec = image_count_spec(form, mode)
    if type(value) is not int or value < spec.minimum or value > spec.maximum:
        label = "主图" if mode == "main" else "详情图"
        raise ImageCountContractError(
            f"{label}张数必须填写 {spec.minimum}–{spec.maximum} 的整数"
        )
    return value


def handheld_count_maximum(mode: str, image_count: int) -> int:
    """Return the hard upper bound after reserving detail module05 as non-handheld."""

    if mode not in SUPPORTED_MODES:
        raise ImageCountContractError("图片类型无效")
    total = _supported_count(image_count)
    return total if mode == "main" else total - 1


def detail_handheld_limit_message(image_count: int) -> str:
    maximum = handheld_count_maximum("detail", image_count)
    return f"含尺寸标注的详情图位不可手持，详情图手持最多 {maximum} 张。"


def chinese_image_count(value: int) -> str:
    """Render the one canonical Chinese number spelling for 1–30."""

    number = _supported_count(value)
    if number < 10:
        return _CHINESE_DIGITS[number]
    if number == 10:
        return "十"
    if number < 20:
        return "十" + _CHINESE_DIGITS[number - 10]
    tens, ones = divmod(number, 10)
    return _CHINESE_DIGITS[tens] + "十" + (_CHINESE_DIGITS[ones] if ones else "")


def config_ids(mode: str, count: int) -> tuple[str, ...]:
    """Generate the only canonical identifier sequence for a real batch."""

    if mode not in SUPPORTED_MODES:
        raise ImageCountContractError("图片类型无效")
    total = _supported_count(count)
    return tuple(f"{mode}_{index:02d}" for index in range(1, total + 1))


def expected_config_ids(main_count: int, detail_count: int) -> tuple[str, ...]:
    return config_ids("main", main_count) + config_ids("detail", detail_count)


def pair_config_ids(mode: str, count: int) -> tuple[tuple[str, ...], ...]:
    identifiers = config_ids(mode, count)
    return tuple(
        identifiers[index : index + 2] for index in range(0, len(identifiers), 2)
    )


def detail_module_groups(count: int) -> tuple[tuple[int, ...], ...]:
    """Map detail slots while omitting dimensions at 1 and assigning them once at 2–30."""

    total = _supported_count(count)
    if total == 1:
        return (_NON_DIMENSION_MODULES,)
    if total < 8:
        independent = tuple((module,) for module in range(1, total - 1))
        merged = (tuple(range(total - 1, 8)),)
        return independent + merged + ((8,),)
    first_eight = tuple((module,) for module in range(1, 9))
    if total == 8:
        return first_eight
    extras = tuple(
        (module,)
        for module in islice(cycle(_DETAIL_EXTRA_MODULE_CYCLE), total - 8)
    )
    return first_eight + extras


def detail_module_assignment_lines(count: int) -> tuple[str, ...]:
    """Render the canonical detail-slot/module mapping for prompts and reports."""

    identifiers = config_ids("detail", count)
    groups = detail_module_groups(count)
    lines: list[str] = []
    for config_id, modules in zip(identifiers, groups, strict=True):
        assignments = " + ".join(
            f"模块{module:02d} {DETAIL_MODULE_LABELS[module - 1]}"
            for module in modules
        )
        lines.append(f"{config_id}：{assignments}")
    return tuple(lines)
