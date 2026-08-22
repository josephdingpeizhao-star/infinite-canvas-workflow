"""Safe per-image QC summaries for Canvas badges."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from codex_dev_downstream import manifest_config_ids
from executor_contract import ExecutorExecutionError
import runtime_roots



class QcSummaryNotFound(FileNotFoundError):
    pass


class QcSummaryInvalid(ValueError):
    pass


def _safe_batch_id(batch_id: str) -> bool:
    return bool(batch_id) and Path(batch_id).name == batch_id and not any(
        char in batch_id for char in ("/", "\\", "\0")
    )


def _asset_config_id(
    value: Any,
    expected_id_set: frozenset[str],
) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.name != value or path.suffix.lower() != ".png":
        return None
    return path.stem if path.stem in expected_id_set else None


def build_qc_summary(
    repository_root: Path,
    batch_id: str,
    *,
    program_root: Path = runtime_roots.PROGRAM_ROOT,
) -> dict[str, Any]:
    if not _safe_batch_id(batch_id):
        raise QcSummaryInvalid("QC 批次标识无效")
    repository_root = repository_root.resolve()
    manifest_path = repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("product_id") != batch_id:
            raise ValueError
        expected_ids = manifest_config_ids(manifest, program_root.resolve())
    except FileNotFoundError:
        raise QcSummaryNotFound("QC 摘要不存在") from None
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        ExecutorExecutionError,
    ):
        raise QcSummaryInvalid("QC 批次图片张数或编号清单无效") from None
    expected_id_set = frozenset(expected_ids)
    expected_assets = frozenset(f"{config_id}.png" for config_id in expected_ids)
    report_path = repository_root / "reports" / f"{batch_id}_qc_report.json"
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
        or len(checked_assets) != len(expected_assets)
        or set(checked_assets) != expected_assets
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
        if result.get("affected_asset") is None:
            continue
        config_id = _asset_config_id(
            result.get("affected_asset"),
            expected_id_set,
        )
        if config_id is None:
            raise QcSummaryInvalid("QC 检查项图位无效")
        if result.get("status") == "needs_review":
            needs_review.add(config_id)

    issue_categories: dict[str, Counter[str]] = {
        config_id: Counter() for config_id in expected_ids
    }
    for issue in issues:
        if not isinstance(issue, dict):
            raise QcSummaryInvalid("QC 问题项无效")
        config_id = _asset_config_id(
            issue.get("affected_asset"),
            expected_id_set,
        )
        category = issue.get("category")
        if config_id is None or not isinstance(category, str) or not category:
            raise QcSummaryInvalid("QC 问题项字段无效")
        issue_categories[config_id][category] += 1

    images: list[dict[str, Any]] = []
    for config_id in expected_ids:
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
        "totalCount": len(expected_ids),
        "expectedConfigIds": list(expected_ids),
        "reportSha256": hashlib.sha256(report_bytes).hexdigest(),
        "images": images,
    }
