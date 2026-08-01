"""Pure metadata and prompt helpers for one bounded content correction round."""

from __future__ import annotations

import re
from dataclasses import dataclass

from executor_contract import ExecutorExecutionError


CONTENT_CORRECTION_CODES = frozenset(
    {
        "unsupported_claims",
        "scene_policy",
        "common_constraints",
        "required_fields",
        "field_content",
        "angle_binding",
        "canvas_ratio",
        "confirmed_height_literal",
        "handheld_reference",
        "module_coverage",
        "module05_handheld",
        "module05_height_literal",
        "module05_forbidden_terms",
        "size_annotation_scope",
        "chunk_coverage",
        "handheld_count",
        "handheld_summary",
    }
)
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ContentViolationDetails:
    """Only the safe, literal data required to correct one predicate failure."""

    config_id: str
    field: str
    expected: str


class ContentPredicateViolation(ExecutorExecutionError):
    """An unchanged public error message plus safe correction metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        config_id: str = "",
        field: str,
        expected: str,
    ) -> None:
        if code not in CONTENT_CORRECTION_CODES or not _CODE_PATTERN.fullmatch(code):
            raise ValueError("invalid content correction code")
        if not field.strip() or not expected.strip():
            raise ValueError("content correction metadata is incomplete")
        self.code = code
        self.details = ContentViolationDetails(
            config_id=config_id.strip(),
            field=field.strip(),
            expected=expected.strip(),
        )
        super().__init__(message)


def build_content_correction_instruction(error: ContentPredicateViolation) -> str:
    """Render only the violation locator, literal condition, and resend boundary."""

    details = error.details
    locator = f"配置 ID：{details.config_id}；" if details.config_id else ""
    return (
        f"{locator}违规字段：{details.field}；必须满足：{details.expected}。"
        "其余内容不变，完整重发本段。"
    )
