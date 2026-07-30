"""Canonical final-prompt literals enforced by the compile-time validator."""

from __future__ import annotations

import re


SUPPORTED_CANVAS_RATIOS = frozenset({"1:1", "3:4"})


def _validated_ratio(expected_ratio: str) -> str:
    ratio = str(expected_ratio).strip()
    if ratio not in SUPPORTED_CANVAS_RATIOS:
        raise ValueError(f"unsupported canvas ratio: {ratio!r}")
    return ratio


def _validated_height_text(height_cm: int) -> str:
    if type(height_cm) is not int:
        raise ValueError("confirmed height must be an integer")
    return str(height_cm)


def required_canvas_ratio_literal(expected_ratio: str) -> str:
    """Return the exact ratio sentence fragment taught to the model."""

    return f"画布比例固定为 {_validated_ratio(expected_ratio)}"


def required_confirmed_height_literal(height_cm: int) -> str:
    """Return the exact confirmed-height sentence fragment taught to the model."""

    return f"高度约 {_validated_height_text(height_cm)} 厘米"


def has_required_canvas_ratio_literal(final_prompt: str, expected_ratio: str) -> bool:
    """Accept only ratio forms that remain inside the independent gate contract."""

    ratio = re.escape(_validated_ratio(expected_ratio))
    return re.search(rf"画布比例固定为\s*{ratio}", final_prompt) is not None


def has_required_confirmed_height_literal(
    final_prompt: str,
    height_cm: int,
) -> bool:
    """Accept only height forms that remain inside the independent gate contract."""

    height = re.escape(_validated_height_text(height_cm))
    return (
        re.search(
            rf"高度[^。；;\n]{{0,24}}约\s*{height}\s*(?:厘米|cm)",
            final_prompt,
        )
        is not None
    )
