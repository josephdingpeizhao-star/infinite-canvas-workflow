"""Safe per-image QC summaries for Canvas badges."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)
CONFIG_ASSETS = frozenset(f"{config_id}.png" for config_id in CONFIG_IDS)


class QcSummaryNotFound(FileNotFoundError):
    pass


class QcSummaryInvalid(ValueError):
    pass


def _safe_batch_id(batch_id: str) -> bool:
    return bool(batch_id) and Path(batch_id).name == batch_id and not any(
        char in batch_id for char in ("/", "\\", "\0")
    )


def _asset_config_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.name != value or path.suffix.lower() != ".png":
        return None
    return path.stem if path.stem in CONFIG_IDS else None


def build_qc_summary(repository_root: Path, batch_id: str) -> dict[str, Any]:
    if not _safe_batch_id(batch_id):
        raise QcSummaryInvalid("QC 批次标识无效")
    report_path = repository_root.resolve() / "reports" / f"{batch_id}_qc_report.json"
    try:
        report_bytes = report_path.read_bytes()
    except FileNotFoundError:
        raise QcSummaryNotFound("QC 摘要不存在") from None
    except OSError:
        raise QcSummaryInvalid("QC 报告不可读取") from None
    try:
        report = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise QcSummaryInvalid("QC 报告格式无效") from None
    if (
        not isinstance(report, dict)
        or report.get("product_id") != batch_id
        or report.get("artifact_type") != "qc_report"
    ):
        raise QcSummaryInvalid("QC 报告批次不匹配")
    checked_assets = report.get("checked_assets")
    if (
        not isinstance(checked_assets, list)
        or len(checked_assets) != len(CONFIG_ASSETS)
        or set(checked_assets) != CONFIG_ASSETS
    ):
        raise QcSummaryInvalid("QC 报告图位集合不完整")
    results = report.get("results")
    issues = report.get("issues")
    if not isinstance(results, list) or not isinstance(issues, list):
        raise QcSummaryInvalid("QC 报告检查结构无效")

    needs_review: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise QcSummaryInvalid("QC 检查项无效")
        config_id = _asset_config_id(result.get("affected_asset"))
        if config_id is None:
            raise QcSummaryInvalid("QC 检查项图位无效")
        if result.get("status") == "needs_review":
            needs_review.add(config_id)

    issue_categories: dict[str, Counter[str]] = {
        config_id: Counter() for config_id in CONFIG_IDS
    }
    for issue in issues:
        if not isinstance(issue, dict):
            raise QcSummaryInvalid("QC 问题项无效")
        config_id = _asset_config_id(issue.get("affected_asset"))
        category = issue.get("category")
        if config_id is None or not isinstance(category, str) or not category:
            raise QcSummaryInvalid("QC 问题项字段无效")
        issue_categories[config_id][category] += 1

    images: list[dict[str, Any]] = []
    for config_id in CONFIG_IDS:
        categories = issue_categories[config_id]
        issue_count = sum(categories.values())
        status = (
            "fail"
            if issue_count
            else "needs_review"
            if config_id in needs_review
            else "pass"
        )
        top_categories = [
            category
            for category, _count in sorted(
                categories.items(), key=lambda item: (-item[1], item[0])
            )[:3]
        ]
        images.append(
            {
                "configId": config_id,
                "status": status,
                "issueCount": issue_count,
                "topCategories": top_categories,
            }
        )
    return {
        "ok": True,
        "batchId": batch_id,
        "reportSha256": hashlib.sha256(report_bytes).hexdigest(),
        "images": images,
    }
